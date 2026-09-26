from abc import ABC, abstractmethod
from dataclasses import Field, dataclass
import json
from typing import Any, Callable, Union
import uuid

from osworld.desktop_env.desktop_env import DesktopEnv
from osworld.mm_agents.old_uitars_agent import UITARSAgent
from osworld.mm_agents.opencua_agent import OpenCUAAgent
import openai
from pydantic import BaseModel
from mm_agents import token_usage
from mm_agents.fides.claude import FidesAnthropicAgent
from mm_agents.fides.opencua import FidesOpenCUAAgent
from mm_agents.fides.uitars import FidesUITARSAgent
from mm_agents.fides.pydantic_scheme import MetaValue
from mm_agents.fides.labels import metadata_to_label
from mm_agents.fides.qllm import QLLMAgent
from mm_agents.fides.tool_scheme import Tool
from mm_agents.fides.lattice import FidesLattice, IntegrityLabel, InverseLattice, PowersetLattice

from openai.types.chat import (
    ChatCompletionMessage, 
    ChatCompletionMessageParam, 
    ChatCompletionToolMessageParam, 
    ChatCompletionAssistantMessageParam, 
    ChatCompletionMessageToolCallParam,
    ChatCompletionToolParam
)

from mm_agents.fides.policies import Policy, PolicyViolation, Action, Query, ToolCall, Response
from mm_agents.fides.tools import QLLMQueryResults, SummarizerOutput

import os
import time
from openai import OpenAI
from mm_agents.openrouter import GROWING, apply_prompt_cache_policy
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
        ), history=GROWING)
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
        metadata = {
            "integrity": IntegrityLabel.trusted(),
            "confidentiality": InverseLattice(
                PowersetLattice(subset=frozenset(), universe=frozenset())
            )
        }
        return QLLMQueryResults(
            root=MetaValue(value=SummarizerOutput(output=content), metadata=metadata)
        )

    tool = {t.name : t for t in tools}[name]
    args = json.loads(json_args)

    print(f"Calling {tool.name} with {args}")

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
        metadata = {
            "integrity": IntegrityLabel.trusted(),
            "confidentiality": InverseLattice(
                PowersetLattice(subset=frozenset(), universe=frozenset())
            )
        }
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
    elif tool.name == "find":
        return tool.callable(
            params, agent, model_defense_level,
            qllm_agent, qllm_agent_second, obs_raw
        )
    elif tool.name == "summarize_screenshot_content":
        return tool.callable(
            params, agent, model_defense_level, 
            qllm_agent, qllm_agent_second, obs_raw
        )
    elif tool.name == "checkdone":
        return tool.callable(
            params, agent, model_defense_level,
            qllm_agent, qllm_agent_second, obs_raw
        )
    elif tool.name in ["get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
        return tool.callable(
            params, model_defense_level, qllm_agent, qllm_agent_second, obs_raw
        )
    elif tool.name == "regex_extract":
        return tool.callable(params)
    else:
        return tool.callable(params, env, example_result_dir, step_idx, agent)
    

def redact_leaves(obj, mapping, history, saving_to_memory, key="root"):
    """
    Recursively walk a dict/list and replace leaf values with appropriate ids.
    """
    if isinstance(obj, dict):
        return {k: redact_leaves(v, mapping, history, saving_to_memory=saving_to_memory, key=k) for k, v in obj.items()}
    if key != "status" and not (
        isinstance(obj, str) 
        and "Your last tool call had an invalid tool name." in obj
    ):
        tool_name = history[-1]["tool_calls"][0]["function"]["name"] if history and "tool_calls" in history[-1] and history[-1]["tool_calls"] else "unknown_tool"
        tool_call_number = sum([1 for msg in history if "tool_calls" in msg and msg["tool_calls"] and msg["tool_calls"][0]["function"]["name"] == tool_name])
        token = f"{tool_name}-result-{tool_call_number}.{key}"
        if saving_to_memory:
            mapping[token] = obj
    else:
        token = obj
    return token

def checking_task_done(history):
    tool_name = history[-1]["tool_calls"][0]["function"]["name"] if history and "tool_calls" in history[-1] and history[-1]["tool_calls"] else "unknown_tool"


class LabeledPlanner(ABC):
    @abstractmethod
    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        """
        Given a message, determine the next action in the planning loop.
        """
        pass

non_ui_tool_list = ["query_llm", "summarize_screenshot_content", "checkdone", "done", "fail", "wait", "find", "get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]

COORDINATE_TOOL_ARGS: dict[str, frozenset[str]] = {
    "click": frozenset({"x", "y"}),
    "move_to": frozenset({"x", "y"}),
    "right_click": frozenset({"x", "y"}),
    "drag_to": frozenset({"x", "y"}),
}

def _reject_literal_coordinate(tool_name: str, arg_name: str) -> None:
    if arg_name in COORDINATE_TOOL_ARGS.get(tool_name, frozenset()):
        raise ValueError(
            f"Argument '{arg_name}' of tool '{tool_name}' must be a 'variable' reference "
            f"(e.g. from find() or find_element_by_text()), not a literal 'value' coordinate."
        )

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

    def _write_planner_state(self):
        """Write planner history and memory incrementally after every step."""
        if not self.example_result_dir:
            return
        with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
            json.dump([msg for msg in self.planner.history], f, indent=2)

        with open(f"{self.example_result_dir}/memory.json", "w", encoding="utf-8") as f:
            json.dump(self.planner.memory, f, indent=2)


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
            if isinstance(action, Query):
                print(f"Action {self.turn}, {label}:\n- {action.messages[-1]}")
            else:
                print(f"Action {self.turn}, {label}:\n- {action}")
            self._write_planner_state()
            trace.append((action, label))

            match action:
                case Query(messages, tools):
                    current_msg = get_tool_response(self.model, messages, tools, token_log_path=self.token_log_path)
                case ToolCall(id, name, arguments):
                    self.turn += 1
                    if action.name not in non_ui_tool_list:
                        self.step_idx += 1
                    result_tool = call_tool(self.tools, name, arguments, self.qllm_agent, self.agent, self.env, self.example_result_dir, self.step_idx, self.observation_type, self.qllm_agent_second, self.model_defense_level)

                    output = getattr(result_tool.root, "output", None)
                    if isinstance(output, str) and output.startswith("Your last tool call had invalid arguments.") and action.name not in non_ui_tool_list:
                        self.step_idx -= 1

                    current_label = metadata_to_label(result_tool.root.metadata).join(current_label)

                    current_msg = ChatCompletionToolMessageParam(role="tool", tool_call_id=id, content=json.dumps(result_tool.model_dump()))

                case Response(response):
                    result = self.env.evaluate(response)
                    if result > 0:
                        with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
                            json.dump([msg for msg in self.planner.history], f, indent=2)
                        with open(f"{self.example_result_dir}/memory.json", "w", encoding="utf-8") as f:
                            json.dump(self.planner.memory, f, indent=2)
                        print(f"Task completed successfully with reward {result}!")
                        return response, current_label
                    else:
                        self.planner.history.append(ChatCompletionToolMessageParam(role="tool", content='{"root": {"status": "FAIL"}}', tool_call_id=response))
                        current_msg = get_tool_response(self.model, messages, tools)
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

    def _expand_args(self, args: dict[str, dict[str, str]], tool_name: str) -> str:
        """
        Expand the arguments to a function call.
        Args:
            args (dict[str, dict[str, str]]): The arguments to expand.
            tool_name (str): The name of the tool being called (used to reject
                literal-value coordinates -- see COORDINATE_TOOL_ARGS above).
        Returns:
            str: The expanded arguments in JSON serialized to a str
        """

        actual_args = {}
        if "gpt" in self.model.lower():
            for a, v in args.items():
                if v['kind'] == 'value':
                    _reject_literal_coordinate(tool_name, a)
                    actual_args[a] = v['value']
                else:
                    if "oss" in self.model.lower():
                        assert v['kind'] == 'variable', f"Invalid kind for argument {a}: {v['kind']}"
                        try: 
                            actual_args[a] = self.memory[v['variable']]
                        except KeyError:
                            if a == "obs":
                                actual_args[a] = {"screenshot": "", "accessibility_tree": ""}
                            else:
                                raise KeyError(f"Variable {v['variable']} not found in memory for argument {a}")
                    elif "gpt-5" in self.model.lower() or "gpt-4" in self.model.lower():
                        assert v['kind'] == 'variable_name', f"Invalid kind for argument {a}: {v['kind']}"
                        try:
                            actual_args[a] = self.memory[v['value']]
                        except KeyError:
                            if a == "obs":
                                actual_args[a] = {"screenshot": "", "accessibility_tree": ""}
                            else:
                                raise KeyError(f"Variable {v['value']} not found in memory for argument {a}")
                            
            return json.dumps(actual_args)
            
        else:
            return self._simple_expand_args(args, tool_name)

    def _simple_expand_args(self, args: dict[str, dict[str, str]], tool_name: str) -> str:
        """
        Resolve each argument's {"kind": "value"/"variable", ...} tag into its
        plain underlying value, per the "Tool Argument Format" every model is
        taught regardless of provider (see build_pllm_prompt in prompts.py).
        Mirrors the "oss" branch of _expand_args's "gpt" path, since that's the
        only convention the live system prompt actually documents.

        This used to recursively walk into every dict and substitute any leaf
        string that happened to match a memory key, without ever collapsing
        the surrounding {"kind": ...} wrapper into a plain scalar. That left
        "value" args wrapped (never unwrapped at all) and "variable" args also
        still wrapped but with their token silently swapped for the resolved
        content one level too deep (e.g. {"kind": "variable", "variable":
        "<actual value>"} instead of just "<actual value>") -- both shapes
        fail Pydantic validation against a tool's plain str/int/etc params.

        Args:
            tool_name: name of the tool being called, used to reject literal-value
                coordinates (see COORDINATE_TOOL_ARGS above) -- checked before the
                "already-plain argument" fallback below, since a bare "x": 960 with
                no "kind" wrapper at all would otherwise slip through untouched.
        """
        actual_args = {}
        for a, v in args.items():
            is_variable_ref = isinstance(v, dict) and v.get("kind") == "variable"
            if not is_variable_ref:
                _reject_literal_coordinate(tool_name, a)
            if not isinstance(v, dict) or "kind" not in v:
                actual_args[a] = v
                continue
            if v["kind"] == "value":
                actual_args[a] = v["value"]
            elif v["kind"] == "variable":
                try:
                    actual_args[a] = self.memory[v["variable"]]
                except KeyError:
                    if a == "obs":
                        actual_args[a] = {"screenshot": "", "accessibility_tree": ""}
                    else:
                        raise KeyError(f"Variable {v['variable']} not found in memory for argument {a}")
            else:
                raise ValueError(f"Invalid kind for argument {a}: {v['kind']}")

        return json.dumps(actual_args)


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

                if (isinstance(content, str) and "Your last tool call had invalid arguments." in content) or (isinstance(content, str) and "Your last tool call had an invalid tool name." in content):
                    content_redacted = content
                else:
                    try:
                        content_dict = json.loads(content)
                        saving_to_memory = True
                        if "status" in content_dict["root"].keys():
                            if content_dict["root"]["status"] == "FAIL":
                                saving_to_memory = False
                        content_redacted = redact_leaves(content_dict, self.memory, self.history, saving_to_memory)
                        if "status" in content_redacted["root"]:
                            if content_redacted["root"]["status"] == "FAIL":
                                if "x" in content_redacted["root"]:
                                    content_redacted["root"]["x"] = "NO_COORDINATES_FOR_FAIL_STATUS"
                                if "y" in content_redacted["root"]:
                                    content_redacted["root"]["y"] = "NO_COORDINATES_FOR_FAIL_STATUS"
                    except:
                        logger.warning(f"Tool output is not valid JSON: {content}. Storing entire output as a single variable.")
                        content_dict = {"root": content}
                        content_redacted = redact_leaves(content_dict, self.memory, self.history, saving_to_memory=True)

                    logger.debug(f"Tool call '{tool_name}' raw (unredacted, hidden-from-planner) content: {content_dict}")

                logger.info(f"Tool call '{tool_name}' returned content: {content_redacted}")
                if tool_name.startswith("checkdone") and isinstance(content_redacted, dict) and "root" in content_redacted and "status" in content_redacted["root"]:
                    if content_redacted["root"]["status"] == "OK":
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
                        arguments=self._expand_args(json.loads(tool_calls[0].function.arguments), tool_calls[0].function.name)
                    ), label
                except Exception as e:
                    content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + str(tool_calls[0]) + ". The error value is redacted for security purposes. Note to always return tool calls arguments with the anyOf format with 'value' and 'variable' kinds."
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