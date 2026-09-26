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
from claude import FidesAnthropicAgent
from opencua import FidesOpenCUAAgent
from uitars import FidesUITARSAgent
from pydantic_scheme import MetaValue
from labels import metadata_to_label
from qllm import QLLMAgent
from tool_scheme import Tool
from lattice import FidesLattice, IntegrityLabel, InverseLattice, PowersetLattice

from openai.types.chat import (
    ChatCompletionMessage, 
    ChatCompletionMessageParam, 
    ChatCompletionToolMessageParam, 
    ChatCompletionAssistantMessageParam, 
    ChatCompletionMessageToolCallParam,
    ChatCompletionToolParam
)

from policies import Policy, PolicyViolation, Action, Query, ToolCall, Response
from tools import QLLMQueryResults, SummarizerOutput, image_bytes_to_base64_str, base64_str_to_image_bytes

import os
import time
from openai import OpenAI
from mm_agents.openrouter import GROWING, apply_prompt_cache_policy
from google import genai
from typing import List, Dict, Any
import logging

logger = logging.getLogger("desktopenv.experiment")

MAX_RETRIES = 15


class ToolSchemaAdapter(ABC):
    """Abstract base class for converting tool schemas to provider-specific formats."""
    
    @abstractmethod
    def convert_tool_schema(self, tool_dict: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """Convert a tool schema to provider-specific format."""
        pass
    
    @abstractmethod
    def convert_parameter_schema(self, param_schema: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """Convert parameter schema (recursive for nested objects)."""
        pass


class OpenAIToolAdapter(ToolSchemaAdapter):
    """Adapter for OpenAI-compatible APIs (OpenAI, OpenRouter)."""
    
    def convert_tool_schema(self, tool_dict: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """OpenAI format already uses anyOf - return as-is."""
        return tool_dict
    
    def convert_parameter_schema(self, param_schema: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """No conversion needed for OpenAI."""
        return param_schema


class GoogleToolAdapter(ToolSchemaAdapter):
    """Adapter for Google Gemini API."""
    
    def convert_tool_schema(self, tool_dict: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """
        Convert OpenAI-style tool schema to Google Gemini format.
        
        Google format:
        {
            "name": "function_name",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        """
        if tool_dict.get("type") == "function":
            func_def = tool_dict["function"]
            
            return {
                "type": "function",
                "function": {
                    "name": func_def["name"],
                    "description": func_def.get("description", ""),
                    "parameters": self.convert_parameter_schema(
                        func_def.get("parameters", {}), 
                        variables
                    )
                }
            }
        
        return tool_dict
    
    def convert_parameter_schema(self, param_schema: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """
        Simplify parameter schema by removing anyOf wrappers.
        
        For each parameter with anyOf[{value}, {variable_name}]:
        - Replace with simple type (string) 
        - Add enum constraint with available variable names
        - Update description to indicate variable usage
        """
        if not isinstance(param_schema, dict):
            return param_schema
        
        converted = param_schema.copy()
        
        if "properties" in converted:
            new_properties = {}
            for prop_name, prop_schema in converted["properties"].items():
                new_properties[prop_name] = self._convert_property(
                    prop_name, prop_schema, variables
                )
            converted["properties"] = new_properties
        
        fields_to_remove = ["$defs", "strict", "additionalProperties", "additional_properties"]
        for field in fields_to_remove:
            if field in converted:
                del converted[field]
            
        return converted
    
    def _convert_property(self, prop_name: str, prop_schema: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """Convert a single property schema."""
        
        if "anyOf" in prop_schema:
            return self._flatten_anyof(prop_name, prop_schema, variables)
        
        if prop_schema.get("type") == "object" and "properties" in prop_schema:
            nested = prop_schema.copy()
            nested["properties"] = {
                k: self._convert_property(k, v, variables)
                for k, v in nested["properties"].items()
            }
            fields_to_remove = ["additionalProperties", "additional_properties", "strict"]
            for field in fields_to_remove:
                if field in nested:
                    del nested[field]
            return nested
        
        if prop_schema.get("type") == "array" and "items" in prop_schema:
            nested = prop_schema.copy()
            nested["items"] = self._convert_property(f"{prop_name}_item", prop_schema["items"], variables)
            return nested
        
        cleaned = prop_schema.copy()
        fields_to_remove = ["additionalProperties", "additional_properties", "strict"]
        for field in fields_to_remove:
            if field in cleaned:
                del cleaned[field]
        
        return cleaned
    
    def _flatten_anyof(self, prop_name: str, prop_schema: Dict[str, Any], variables: List[str]) -> Dict[str, Any]:
        """
        Flatten anyOf[{value}, {variable_name}] to simple string type.
        
        Strategy:
        - Use string type (can hold both literal values and variable names)
        - Add description explaining variable usage
        - Optionally add enum if we want to restrict to known variables
        """
        anyof_schemas = prop_schema.get("anyOf", [])
        
        base_type = "string"
        for schema in anyof_schemas:
            if schema.get("properties", {}).get("kind", {}).get("const") == "value":
                value_schema = schema.get("properties", {}).get("value", {})
                base_type = value_schema.get("type", "string")
                break
        
        original_desc = prop_schema.get("description", "")
        variable_hint = (
            f"\n\nYou can pass either:\n"
            f"1. A literal {base_type} value\n"
            f"2. A variable name from previous tool outputs (e.g., 'find-result-1.x')\n"
        )
        
        if variables:
            variable_hint += f"\nAvailable variables: {', '.join(variables[:20])}"
            if len(variables) > 20:
                variable_hint += f" (and {len(variables) - 20} more)"
        
        new_schema = {
            "type": "string",
            "description": (original_desc + variable_hint).strip()
        }
        
        
        return new_schema


def get_tool_adapter(provider: str) -> ToolSchemaAdapter:
    """Factory function to get the appropriate adapter."""
    adapters = {
        "openai": OpenAIToolAdapter(),
        "openrouter": OpenAIToolAdapter(),
        "google": GoogleToolAdapter(),
    }
    
    if provider not in adapters:
        raise ValueError(f"Unknown provider '{provider}'. Supported: {', '.join(adapters.keys())}")
    
    return adapters[provider]


def convert_tools_for_provider(
    tools: List[Dict[str, Any]], 
    provider: str, 
    variables: List[str]
) -> List[Dict[str, Any]]:
    """
    Convert a list of tool schemas for the specified provider.
    
    Args:
        tools: List of tool schemas in OpenAI format with anyOf patterns
        provider: Provider name ('openai', 'google', 'openrouter')
        variables: List of available variable names from memory
    
    Returns:
        List of converted tool schemas
    """
    adapter = get_tool_adapter(provider)
    return [adapter.convert_tool_schema(tool, variables) for tool in tools]



def get_tool_response(model: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
    """
    Send a tool-using chat request to the appropriate model provider (openai, openrouter, google).
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
        return _retry_openai_style(api_func, messages, provider)

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
        return _retry_openai_style(api_func, messages, provider)

    elif provider == "google":
        return _handle_google_request(model_name, messages, tools)

    else:
        raise ValueError(f"Unknown provider '{provider}'. Supported: openai, openrouter, google")


def _retry_openai_style(api_func, messages, provider):
    """Handle retry logic for OpenAI-style APIs."""
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            response = api_func()
            current_msg = response.choices[0].message
            
            if getattr(current_msg, "tool_calls", None):
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
        except Exception as e:
            print(f"[{provider.upper()} Attempt {attempt + 1}] Error: {e}")
            time.sleep(1)
        
        attempt += 1
        print(f"[{provider.upper()} Retry {attempt}] No valid tool call detected. Retrying...")
    
    raise RuntimeError(f"Model failed to produce a valid tool call after {MAX_RETRIES} attempts.")


def _handle_google_request(model_name: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
    """
    Handle Google Gemini API requests with proper format conversion.
    
    Google Gemini uses a different message format:
    - Messages are called "contents"
    - Tools are passed as "tools" parameter with function_declarations
    - Function calling response format differs
    """
    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    
    google_contents = _convert_messages_to_google_format(messages)
    
    function_declarations = []
    for tool in tools:
        if tool.get("type") == "function":
            func = tool["function"]
            function_declarations.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {})
            })
    
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            config = {
                "temperature": 1.0,
            }
            
            if function_declarations:
                config["tools"] = [{"function_declarations": function_declarations}]
                config["tool_config"] = {"function_calling_config": {"mode": "ANY"}}
            
            response = client.models.generate_content(
                model=model_name,
                contents=google_contents,
                config=config
            )
            
            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'function_call') and part.function_call:
                        return _convert_google_function_call_to_openai(part.function_call)
            
            print(f"[GOOGLE Attempt {attempt + 1}] No function call in response")
            
        except Exception as e:
            print(f"[GOOGLE Attempt {attempt + 1}] Error: {e}")
            time.sleep(1)
        
        attempt += 1
        print(f"[GOOGLE Retry {attempt}] No valid tool call detected. Retrying...")
    
    raise RuntimeError(f"Google model failed to produce a valid tool call after {MAX_RETRIES} attempts.")


def _convert_messages_to_google_format(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert OpenAI-style messages to Google Gemini format.
    
    OpenAI: {"role": "user", "content": "..."}
    Google: {"role": "user", "parts": [{"text": "..."}]}
    """
    google_messages = []
    
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        
        if role == "assistant":
            google_role = "model"
        elif role == "user":
            google_role = "user"
        elif role == "tool":
            google_role = "user"
            google_messages.append({
                "role": google_role,
                "parts": [{
                    "function_response": {
                        "name": msg.get("name", "unknown"),
                        "response": {"result": content}
                    }
                }]
            })
            continue
        else:
            google_role = "user"
        
        if "tool_calls" in msg and msg["tool_calls"]:
            tool_call = msg["tool_calls"][0]
            google_messages.append({
                "role": "model",
                "parts": [{
                    "function_call": {
                        "name": tool_call["function"]["name"],
                        "args": json.loads(tool_call["function"]["arguments"])
                    }
                }]
            })
        else:
            google_messages.append({
                "role": google_role,
                "parts": [{"text": str(content)}]
            })
    
    return google_messages


def _convert_google_function_call_to_openai(function_call):
    """Convert Google function call format to OpenAI ChatCompletionMessage format."""
    import uuid
    
    class MockToolCall:
        def __init__(self, name, args):
            self.id = f"call_{uuid.uuid4().hex[:24]}"
            self.type = "function"
            self.function = type('obj', (object,), {
                'name': name,
                'arguments': json.dumps(args)
            })()
    
    class MockMessage:
        def __init__(self, tool_call):
            self.role = "assistant"
            self.content = None
            self.tool_calls = [tool_call]
    
    tool_call = MockToolCall(
        name=function_call.name,
        args=dict(function_call.args)
    )
    
    return MockMessage(tool_call)



def call_tool(
        tools: list[Tool],
        name: str,
        json_args: str,
        qllm_agent: QLLMAgent,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        env: DesktopEnv, 
        example_result_dir: str,
        step_idx: int,
        observation_type: str
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

    if tool.name in ["run_vlm", "summarize_screenshot_content", "find", "checkdone"]:
        obs = env._get_obs()
        args["obs"] = {"screenshot": "", "accessibility_tree": ""}
        args["obs"]["screenshot"] = image_bytes_to_base64_str(obs["screenshot"])
    if tool.name in ["get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
        obs = env._get_obs()
        args["accessibility_tree"] = obs["accessibility_tree"]
    if tool.name == "summarize_screenshot_content":
        if "output_type" not in args:
            args["output_type"] = "str"

    try:
        params = tool.parameter_model(**args)
    except:
        content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + "name" + str(name) + " with args " + str(args) + "."

        metadata = {
            "integrity": IntegrityLabel.trusted(),
            "confidentiality": InverseLattice(
                PowersetLattice(subset=frozenset(), universe=frozenset())
            )
        }
        return QLLMQueryResults(
            root=MetaValue(value=SummarizerOutput(output=content), metadata=metadata)
        )

    
    if tool.name == "query_llm" :
        return tool.callable(params, qllm_agent, observation_type)
    elif tool.name == 'run_vlm':
        return tool.callable(params, agent, qllm_agent, env, example_result_dir, step_idx, observation_type)
    elif tool.name == "find":
        return tool.callable(params, agent, qllm_agent, observation_type)
    elif tool.name in ["summarize_screenshot_content", "checkdone"]:
        return tool.callable(params, agent, observation_type)
    elif tool.name in ["get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
        return tool.callable(params, qllm_agent)
    elif tool.name == "regex_extract":
        return tool.callable(params)
    else:
        return tool.callable(params, env, example_result_dir, step_idx, agent)


def redact_leaves(obj, mapping, history, key="root"):
    """
    Recursively walk a dict/list and replace leaf values with appropriate ids.
    """
    if isinstance(obj, dict):
        return {k: redact_leaves(v, mapping, history, key=k) for k, v in obj.items()}
    if key != "status":
        tool_name = history[-1]["tool_calls"][0]["function"]["name"] if history and "tool_calls" in history[-1] and history[-1]["tool_calls"] else "unknown_tool"
        tool_call_number = sum([1 for msg in history if "tool_calls" in msg and msg["tool_calls"] and msg["tool_calls"][0]["function"]["name"] == tool_name])
        token = f"{tool_name}-result-{tool_call_number}.{key}"
        mapping[token] = obj
    else:
        token = obj
    return token



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
            observation_type: str = "screenshot_a11y_tree"):
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
        
    def loop(self, msg: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[str, FidesLattice]:
        """
        Run the planning loop with dynamic taint-tracking.

        May raise PolicyViolation if the policy is violated.
        """

        trace: list[tuple[Action, FidesLattice]] = []
        current_msg = msg
        current_label = label
        while self.max_steps is None or self.step_idx < self.max_steps:
            if current_msg == None:
                pass

            result = self.env.evaluate()
            if result > 0:
                print(f"Task completed successfully with reward {result}!")
                return "done", current_label
            
            action, label = self.planner.next_action(current_msg, current_label)
            trace.append((action, label))

            match action:
                case Query(messages, tools):
                    current_msg = get_tool_response(self.model, messages, tools)
                case ToolCall(id, name, arguments):
                    self.turn += 1
                    if action.name not in ["query_llm", "summarize_screenshot_content", "checkdone", "done", "fail", "wait", "find", "get_page_elements", "get_page_text", "find_element_by_text", "verify_hypothesis"]:
                        self.step_idx += 1
                    result_tool = call_tool(self.tools, name, arguments, self.qllm_agent, self.agent, self.env, self.example_result_dir, self.step_idx, self.observation_type)
                    current_label = metadata_to_label(result_tool.root.metadata).join(current_label)

                    current_msg = ChatCompletionToolMessageParam(role="tool", tool_call_id=id, content=json.dumps(result_tool.model_dump()))

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
    def __init__(self, state: list[ChatCompletionMessageParam], tools: list[Tool], model: str):
        self.tools = tools
        self.history = state
        self.memory: dict[str, Any] = {}
        self.model = model
        
        try:
            provider, model_name = model.split(":", 1)
            if provider == "google":
                self.provider = "google"
                self.api_provider = "google"
            else:
                self.provider = "openai"
                self.api_provider = provider
        except ValueError:
            self.provider = "openai"
            self.api_provider = "openai"

    def _expand_args(self, args: dict[str, dict[str, str]]) -> str:
        """
        Expand the arguments to a function call.
        Args:
            args (dict[str, dict[str, str]]): The arguments to expand.
        Returns:
            str: The expanded arguments in JSON serialized to a str
        """

        actual_args = {}
        for a, v in args.items(): 
            if v['kind'] == 'value':
                actual_args[a] = v['value']
            else:
                assert v['kind'] == 'variable', f"Invalid kind for argument {a}: {v['kind']}"
                try: 
                    actual_args[a] = self.memory[v['variable']]
                except KeyError:
                    if a == "obs":
                        actual_args[a] = {"screenshot": "", "accessibility_tree": ""}
                    else:
                        raise KeyError(f"Variable {v['variable']} not found in memory for argument {a}")
        return json.dumps(actual_args)
    
    def _simple_expand_args(self, args: dict[str, str]) -> str:
        """
        Recursively expand arguments by replacing variable names with
        their values stored in self.memory.
        
        Handles multiple formats:
        - OpenAI format: {param: {kind: "value", value: ...}} or {param: {kind: "variable_name", value: "var-1"}}
        - Direct format (for direct Google API): {param: "var-1"} or {param: 123}
        """
        def expand(value):
            if isinstance(value, dict):
                if "kind" in value and "value" in value:
                    if value["kind"] == "value":
                        return value["value"]
                    elif value["kind"] == "variable_name":
                        var_name = value["value"]
                        if var_name in self.memory:
                            return self.memory[var_name]
                        return var_name
                return {k: expand(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [expand(item) for item in value]
            elif isinstance(value, str):
                if value in self.memory:
                    return self.memory[value]
            return value

        expanded = expand(args)
        return json.dumps(expanded)


    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        
        tools = convert_tools_for_provider(
            [tool.to_dict_var(list(self.memory.keys())) for tool in self.tools],
            self.provider,
            list(self.memory.keys())
        )

        match message:
            case {"role": "user"}:
                self.history.append(message)
                return Query(
                    messages=self.history,
                    tools=tools
                ), label
            case {"role": "tool", "content": content, "tool_call_id": tool_call_id}:
                try:
                    content_dict = json.loads(content)
                    content_redacted = redact_leaves(content_dict, self.memory, self.history)
                except:
                    logger.warning(f"Tool output is not valid JSON: {content}. Storing entire output as a single variable.")
                    content_dict = {"root": content}
                    content_redacted = redact_leaves(content_dict, self.memory, self.history)


                self.history.append(ChatCompletionToolMessageParam(role="tool", content=str(content_redacted), tool_call_id=tool_call_id))
                return Query(
                    messages=self.history,
                    tools=tools
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
                        arguments=self._simple_expand_args(json.loads(tool_calls[0].function.arguments))
                    ), label
                except Exception as e:
                    content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + str(tool_calls[0]) + ". The error value is redacted for security purposes. Note to always return tool calls arguments with the anyOf format with 'value' and 'variable' kinds."
                    self.history.append(ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=None))
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