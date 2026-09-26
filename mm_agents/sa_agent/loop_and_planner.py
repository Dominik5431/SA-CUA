from abc import ABC, abstractmethod
from dataclasses import dataclass
import ast
import json
import logging
import re
import uuid
from typing import Any, Callable, Union

from osworld.desktop_env.desktop_env import DesktopEnv
from osworld.mm_agents.old_uitars_agent import UITARSAgent
from osworld.mm_agents.opencua_agent import OpenCUAAgent
import openai
from pydantic import BaseModel
from mm_agents import token_usage
from mm_agents.sa_agent.claude import FidesAnthropicAgent
from mm_agents.sa_agent.opencua import FidesOpenCUAAgent
from mm_agents.sa_agent.uitars import FidesUITARSAgent
from mm_agents.sa_agent.pydantic_scheme import MetaValue
from mm_agents.sa_agent.labels import metadata_to_label
from mm_agents.sa_agent.qllm import QLLMAgent
from mm_agents.sa_agent.tool_scheme import Tool
from mm_agents.sa_agent.lattice import FidesLattice

from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageToolCallParam,
    ChatCompletionToolParam
)
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)

from mm_agents.sa_agent.policies import Policy, PolicyViolation, Action, Query, ToolCall, Response

logger = logging.getLogger("desktopenv.experiment")


MAX_RETRIES = 5

import os
import time
from openai import OpenAI
from mm_agents.openrouter import GROWING, apply_prompt_cache_policy
from google import genai
from typing import List, Dict, Any

MAX_RETRIES = 10


_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_FUNCTION_RE = re.compile(r"<function=(.*?)</function>|<function=(.*)$", re.DOTALL)
_PARAMETER_RE = re.compile(
    r"<parameter=(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)", re.DOTALL
)


def _coerce_param_value(raw_value: str, param_name: str, param_schema: Dict[str, Any]) -> Any:
    """
    Best-effort type coercion mirroring Qwen3CoderToolParser._convert_param_value, so
    fallback-recovered arguments end up the same JSON types (int/float/bool/object) the
    real server-side tool-call parser would have produced, instead of always raw strings.
    """
    if raw_value.strip().lower() == "null":
        return None

    prop = param_schema.get(param_name)
    param_type = "string"
    if isinstance(prop, dict) and "type" in prop:
        param_type = str(prop["type"]).strip().lower()
    elif isinstance(prop, dict) and "anyOf" in prop:
        param_type = "object"

    if param_type in ("string", "str", "text", "varchar", "char", "enum"):
        return raw_value
    if param_type.startswith(("int", "uint", "long", "short", "unsigned")):
        try:
            return int(raw_value)
        except ValueError:
            return raw_value
    if param_type.startswith(("num", "float")):
        try:
            f = float(raw_value)
            return int(f) if f.is_integer() else f
        except ValueError:
            return raw_value
    if param_type in ("boolean", "bool", "binary"):
        return raw_value.strip().lower() == "true"
    if param_type in ("object", "array", "arr") or param_type.startswith(("dict", "list")):
        try:
            return json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        return ast.literal_eval(raw_value)
    except (ValueError, SyntaxError):
        return raw_value


def _parse_embedded_tool_call(
        text: str,
        tools: List[Dict[str, Any]]
    ) -> ChatCompletionMessageFunctionToolCall | None:
    """
    Recover a <tool_call><function=NAME><parameter=KEY>VALUE</parameter>...</function></tool_call>
    block (Qwen3-coder-style XML) from raw text. Returns None if no well-formed tool call
    is found. Only the first tool call in `text` is parsed (the rest of the pipeline only
    ever consumes a single tool call per turn, see the "Only one tool call is supported"
    handling in LabeledBasicPlanner.next_action).
    """
    if not text or "<tool_call>" not in text:
        return None

    call_match = _TOOL_CALL_RE.search(text)
    if not call_match:
        return None

    func_match = _FUNCTION_RE.search(call_match.group(1))
    if not func_match:
        return None
    function_block = func_match.group(1) if func_match.group(1) is not None else func_match.group(2)

    end_index = function_block.find(">")
    if end_index == -1:
        return None
    function_name = function_block[:end_index].strip()
    parameters_text = function_block[end_index + 1:]

    param_schema = {}
    for tool in tools:
        fn = tool.get("function", {})
        if fn.get("name") == function_name:
            param_schema = fn.get("parameters", {}).get("properties", {}) or {}
            break

    param_dict = {}
    for match_text in _PARAMETER_RE.findall(parameters_text):
        idx = match_text.find(">")
        if idx == -1:
            continue
        param_name = match_text[:idx].strip()
        param_value = match_text[idx + 1:]
        if param_value.startswith("\n"):
            param_value = param_value[1:]
        if param_value.endswith("\n"):
            param_value = param_value[:-1]
        param_dict[param_name] = _coerce_param_value(param_value, param_name, param_schema)

    return ChatCompletionMessageFunctionToolCall(
        id=f"call_{uuid.uuid4().hex[:24]}",
        type="function",
        function=Function(name=function_name, arguments=json.dumps(param_dict, ensure_ascii=False)),
    )


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

    while attempt < MAX_RETRIES:
        try:
            response = api_func()

            if provider in ("openai", "openrouter", "vllm"):
                current_msg = response.choices[0].message

                if token_log_path is not None:
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                        total_tokens = usage.get("total_tokens", 0)
                        with open(token_log_path, "a") as f:
                            f.write(f"Model {model_name}, Function planner - Input tokens: {prompt_tokens}, Output tokens: {completion_tokens}, Total tokens: {total_tokens}\n")

                if getattr(current_msg, "tool_calls", None):
                    return current_msg

                reasoning_text = getattr(current_msg, "reasoning_content", None) or getattr(current_msg, "reasoning", None)
                if reasoning_text:
                    recovered_tool_call = _parse_embedded_tool_call(reasoning_text, tools)
                    if recovered_tool_call is not None:
                        logger.info(
                            "get_tool_response: server-side tool-call parsing returned no "
                            "tool_calls, but the client-side fallback parser recovered a "
                            "'%s' call from the model's reasoning output. Arguments: %s",
                            recovered_tool_call.function.name,
                            recovered_tool_call.function.arguments,
                        )
                        print(
                            f"[Fallback] Server returned no tool_calls; recovered "
                            f"'{recovered_tool_call.function.name}' from reasoning output "
                            f"with arguments {recovered_tool_call.function.arguments}"
                        )
                        current_msg.tool_calls = [recovered_tool_call]
                        return current_msg

                if getattr(current_msg, "content", None) and not current_msg.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": (
                            "Your last response did not include a tool call. "
                            "ALL RESPONSES SHOULD INCLUDE A TOOL CALL. "
                            "Use the provided tools to interact with the environment."
                        )
                    })

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
        model_defense_level: int = 0
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
        raise ValueError(f"Unknown tool: {name}")

    tool = {t.name : t for t in tools}[name]

    args = json.loads(json_args)

    print(f"Calling {tool.name} with {args}")

    obs_raw = None
    if tool.name in ["run_vlm", "summarize_screenshot_content", "find", "checkdone",
                      "get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
        obs_raw = env._get_obs()

    if tool.name == "summarize_screenshot_content":
        if "output_type" not in args:
            args["output_type"] = "str"
    params = tool.parameter_model(**args)

    if tool.name == "query_llm" :
        return tool.callable(params, qllm_agent, observation_type)
    elif tool.name == 'run_vlm':
        return tool.callable(params, agent, qllm_agent, env, example_result_dir, step_idx, observation_type, model_defense_level, None)
    elif tool.name == "find":
        return tool.callable(params, agent, model_defense_level, qllm_agent, None, obs_raw)
    elif tool.name in ["summarize_screenshot_content", "checkdone"]:
        return tool.callable(params, agent, model_defense_level, qllm_agent, None, obs_raw)
    elif tool.name in ["get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
        return tool.callable(params, model_defense_level, qllm_agent, None, obs_raw)
    elif tool.name == "regex_extract":
        return tool.callable(params)
    else:
        return tool.callable(params, env, example_result_dir, step_idx, agent)




class LabeledPlanner(ABC):
    @abstractmethod
    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        """
        Given a message, determine the next action in the planning loop.
        """
        pass


class LabeledPlanningLoop:
    def __init__(
            self,
            planner: LabeledPlanner,
            model: str,
            tools: list[Tool],
            policy: Policy,
            qllm_agent: QLLMAgent,
            agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
            env: DesktopEnv,
            example_result_dir: str = "",
            step_idx: int = 0,
            max_steps: int = 15,
            observation_type: str = "screenshot_a11y_tree",
            token_log_path: str | None = None,
            model_defense_level: int = 0,
        ):
        self.planner = planner
        self.model = model
        self.tools = tools
        self.policy = policy
        self.qllm_agent = qllm_agent
        self.agent = agent
        self.env = env
        self.step_idx = step_idx
        self.turn = 0
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.observation_type = observation_type
        self.token_log_path = token_log_path
        self.model_defense_level = model_defense_level
        
    def loop(self, msg: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[str, FidesLattice]:
        """
        Run the planning loop with dynamic taint-tracking.

        May raise PolicyViolation if the policy is violated.
        """

        trace: list[tuple[Action, FidesLattice]] = []
        current_msg = msg
        current_label = label
        while self.max_steps is None or self.step_idx < self.max_steps:
            token_usage.set_step(self.step_idx + 1)
            if current_msg == None:
                breakpoint()
            action, label = self.planner.next_action(current_msg, current_label)
            print(f"Action {self.turn}, {label}:\n- {action}")
            trace.append((action, label))


            match action:
                case Query(messages, tools):
                    current_msg = get_tool_response(self.model, messages, tools, token_log_path=self.token_log_path)
                case ToolCall(id, name, arguments):
                    self.turn += 1
                    if action.name not in ["query_llm", "summarize_screenshot_content", "checkdone", "done", "fail", "wait", "find", "get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
                        self.step_idx += 1
                    result = call_tool(self.tools, name, arguments, self.qllm_agent, self.agent, self.env, self.example_result_dir, self.step_idx, self.observation_type, self.model_defense_level)
                    current_msg = ChatCompletionToolMessageParam(role="tool", tool_call_id=id, content=str(result))
                    current_label = metadata_to_label(result.root.metadata).join(current_label)
                    if name in ("done", "fail", "abstain"):
                        self.planner.history.append(current_msg)
                        if self.example_result_dir:
                            with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
                                json.dump([msg for msg in self.planner.history], f, indent=2)
                        return str(current_msg["content"]), current_label
                case Response(response):
                    if self.example_result_dir:
                        with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
                            json.dump([msg for msg in self.planner.history], f, indent=2)
                    return response, current_label
                case _:
                    raise ValueError("Invalid action")
        if self.example_result_dir:
            with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
                json.dump([msg for msg in self.planner.history], f, indent=2)
        return "", current_label 


class LabeledBasicPlanner(LabeledPlanner):
    def __init__(self, state: list[ChatCompletionMessageParam], tools: list[Tool]):
        self.tools = tools
        self.history = state

    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        match message:
            case {"role": "user"} | {"role": "tool"}:
                self.history.append(message)
                return Query(
                    messages=self.history,
                    tools=[ tool.to_dict() for tool in self.tools ]
                ), label
            case ChatCompletionMessage(role = "assistant", content = content, tool_calls = tool_calls) if tool_calls:
                if len(tool_calls) != 1:
                    print("Warning: Only one tool call is supported. Using the first tool call from the list.")
                    tool_calls = [tool_calls[0]] if tool_calls else None
                try:
                    tool = {t.name : t for t in self.tools}[tool_calls[0].function.name]
                    params = tool.parameter_model(**json.loads(tool_calls[0].function.arguments))
                except:
                    content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + str(tool_calls[0])
                    self.history.append(ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=None))
                    return Query(
                        messages=self.history,
                        tools=[ tool.to_dict() for tool in self.tools ]
                    ), label
                
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
                return ToolCall(
                    id=tool_calls[0].id,
                    name=tool_calls[0].function.name,
                    arguments=tool_calls[0].function.arguments
                ), label
            case ChatCompletionMessage(role = "assistant", content = content, tool_calls = tool_calls) if content:
                assert not tool_calls, "Tool calls are not supported in this context"
                self.history.append(ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=None))
                return Response(
                    response=content
                ), label
            case _:
                breakpoint()
                raise ValueError("Invalid message format")