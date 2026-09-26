from dataclasses import dataclass
from typing import Callable, Any
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from mm_agents.fides.lattice import Lattice


@dataclass
class Action:
    pass

@dataclass
class Query(Action):
    messages : list[ChatCompletionMessageParam]
    tools: list[ChatCompletionToolParam]

@dataclass
class ToolCall(Action):
    id: str
    name: str
    arguments: str

@dataclass
class Response(Action):
    response: str

class PolicyViolation(Exception):
    def __init__(self, reason: str):
        super().__init__(f"Policy violation: {reason}")
        self.reason = reason

Policy = Callable[[list[tuple[Action, Lattice]]], None]


def policy_no_untrusted_url(trace: list[tuple[Action, Lattice]]) -> None:

    pass
