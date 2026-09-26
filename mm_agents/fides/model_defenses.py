
from mm_agents.fides.lattice import IntegrityLabel, InverseLattice, PowersetLattice
from mm_agents.fides.pydantic_scheme import MetaValue
from mm_agents.fides.qllm import QLLMAgent
from pydantic import BaseModel, ValidationError
from typing import Any, List
import json
import logging
import re

logger = logging.getLogger("desktopenv.experiment")

class PromptInjectionCall(BaseModel):
    detected: str
    str_messages: List[str]

def extract_json_from_text(text: str) -> str:
    match = re.search(r'{.*}', text, re.DOTALL)
    return match.group(0) if match else text

def parse_prompt_injection(output_str: str) -> PromptInjectionCall:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return PromptInjectionCall(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Failed to parse prompt injection output: {e}")
        return PromptInjectionCall(
            detected="SAFE",
            str_messages=["Parsing error - defaulting to SAFE"]
        )



class ModelDefenseParams(BaseModel):
    instruction: MetaValue[str]
    thought: str
    action: str
    dom: str = ""


class ModelDefenseResults(BaseModel):
    root: MetaValue[PromptInjectionCall]



def model_defense_0(
        params: ModelDefenseParams,
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent = None,
        screenshot: Any = None
) -> ModelDefenseResults:
    """
    Model defense level 0: no defense applied.
    
    This function always returns SAFE without performing any checks.
    Use for baseline testing or when security checks are not needed.
    
    Args:
        params: Defense parameters (unused).
        qllm_agent: The quarantined LLM agent (unused).
    
    Returns:
        ModelDefenseResults indicating SAFE with no defense applied.
    """
    result = PromptInjectionCall(
        detected="SAFE",
        str_messages=["No defense applied (level 0)."]
    )
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return ModelDefenseResults(
        root=MetaValue(value=result, metadata=metadata)
    )


def model_defense_1(
        params: ModelDefenseParams,
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent = None,
        screenshot: Any = None
) -> ModelDefenseResults:
    """
    Model defense level 1: check thought and action against instruction only.
    
    This is the lightest security check - only analyzes the instruction, thought,
    and action without considering the environment (no DOM or screenshot).
    
    Args:
        params: Defense parameters with instruction, thought, and action.
        qllm_agent: The quarantined LLM agent to perform the check.
    
    Returns:
        ModelDefenseResults with prompt injection detection result.
    """
    response = qllm_agent.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom="",
        screenshot=""
    )
    
    parsed_response = parse_prompt_injection(response)
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return ModelDefenseResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )


def model_defense_2(
        params: ModelDefenseParams,
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent = None,
        screenshot: Any = None
) -> ModelDefenseResults:
    """
    Model defense level 2: check with instruction and DOM.
    
    Includes DOM/accessibility tree analysis to detect if the agent is
    following injected instructions from malicious UI elements.
    
    Args:
        params: Defense parameters with instruction, thought, action, and DOM.
        qllm_agent: The quarantined LLM agent to perform the check.
    
    Returns:
        ModelDefenseResults with prompt injection detection result.
    """
    response = qllm_agent.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom=params.dom,
        screenshot=""
    )
    
    parsed_response = parse_prompt_injection(response)
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return ModelDefenseResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )


def model_defense_3(
        params: ModelDefenseParams,
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent,
        screenshot: Any
) -> ModelDefenseResults:
    """
    Model defense level 3: dual check with DOM and screenshot separately.
    
    Performs two independent checks:
    1. Check with DOM only (using primary qllm_agent)
    2. Check with screenshot only (using secondary qllm_agent_second)
    
    Both checks must pass for the action to be considered SAFE.
    
    Args:
        params: Defense parameters with instruction, thought, action, DOM, and screenshot.
        qllm_agent: Primary quarantined LLM agent for DOM check.
        qllm_agent_second: Secondary quarantined LLM agent for screenshot check.
    
    Returns:
        ModelDefenseResults with combined detection result.
    """
    dom_response = qllm_agent.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom=params.dom,
        screenshot=""
    )
    dom_result = parse_prompt_injection(dom_response)
    
    screenshot_response = qllm_agent_second.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom="",
        screenshot=screenshot
    )
    screenshot_result = parse_prompt_injection(screenshot_response)
    
    if dom_result.detected == "SAFE" and screenshot_result.detected == "SAFE":
        final_result = PromptInjectionCall(
            detected="SAFE",
            str_messages=["Both DOM and screenshot checks passed."]
        )
    elif dom_result.detected == "ATTACKED" and screenshot_result.detected == "ATTACKED":
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=["Both DOM and screenshot checks detected attack."]
        )
    elif dom_result.detected == "ATTACKED":
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=[f"DOM check detected attack: {dom_result.str_messages[0]}"]
        )
    else:
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=[f"Screenshot check detected attack: {screenshot_result.str_messages[0]}"]
        )
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return ModelDefenseResults(
        root=MetaValue(value=final_result, metadata=metadata)
    )


def model_defense_4(
        params: ModelDefenseParams,
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent,
        screenshot: Any
) -> ModelDefenseResults:
    """
    Model defense level 4: comprehensive dual check with all information.
    
    Performs two independent checks:
    1. Full check with DOM and screenshot (using primary qllm_agent)
    2. Screenshot-only check (using secondary qllm_agent_second)
    
    This is the strongest defense level. Both checks must pass for SAFE.
    
    Args:
        params: Defense parameters with instruction, thought, action, DOM, and screenshot.
        qllm_agent: Primary quarantined LLM agent for full check.
        qllm_agent_second: Secondary quarantined LLM agent for screenshot verification.
    
    Returns:
        ModelDefenseResults with combined detection result.
    """
    first_response = qllm_agent.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom=params.dom,
        screenshot=screenshot
    )
    first_result = parse_prompt_injection(first_response)
    
    second_response = qllm_agent_second.checker_base(
        instruction=params.instruction.value,
        thought=params.thought,
        action=params.action,
        dom="",
        screenshot=screenshot
    )
    second_result = parse_prompt_injection(second_response)
    
    if first_result.detected == "SAFE" and second_result.detected == "SAFE":
        final_result = PromptInjectionCall(
            detected="SAFE",
            str_messages=["Both comprehensive and screenshot checks passed."]
        )
    elif first_result.detected == "ATTACKED" and second_result.detected == "ATTACKED":
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=["Both comprehensive and screenshot checks detected attack."]
        )
    elif first_result.detected == "ATTACKED":
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=[f"Comprehensive check detected attack: {first_result.str_messages[0]}"]
        )
    else:
        final_result = PromptInjectionCall(
            detected="ATTACKED",
            str_messages=[f"Screenshot verification detected attack: {second_result.str_messages[0]}"]
        )
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return ModelDefenseResults(
        root=MetaValue(value=final_result, metadata=metadata)
    )

MODEL_DEFENSE_LEVELS = {
    0: model_defense_0,
    1: model_defense_1,
    2: model_defense_2,
    3: model_defense_3,
    4: model_defense_4,
}


def apply_model_defense(
        instruction: MetaValue[str],
        thought: str,
        action: str,
        dom: str,
        screenshot: str, 
        qllm_agent: QLLMAgent,
        qllm_agent_second: QLLMAgent,
        model_defense_level: int = 0
) -> ModelDefenseResults:
    """
    Apply the selected model defense level to the given parameters.
    
    Args:
        params: Defense parameters including instruction, thought, action, DOM, and screenshot.
        qllm_agent: Primary quarantined LLM agent.
        qllm_agent_second: Secondary quarantined LLM agent (if needed).
    
    Returns:
        ModelDefenseResults with prompt injection detection result.
    """
    if model_defense_level >= 2 and not dom:
        logger.warning(
            "Model defense level %d relies on the accessibility tree (DOM) to detect "
            "prompt injection, but no accessibility tree was provided for this check. "
            "The DOM-based portion of this defense will run with an empty DOM and provide "
            "no real protection -- likely because the agent is running with "
            "observation_type=screenshot instead of screenshot_a11y_tree.",
            model_defense_level
        )

    defense_function = MODEL_DEFENSE_LEVELS.get(model_defense_level, model_defense_0)
    params = ModelDefenseParams(
        instruction=instruction,
        thought=thought,
        action=action,
        dom=dom
    )
    return defense_function(
        params=params,
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        screenshot=screenshot
    )