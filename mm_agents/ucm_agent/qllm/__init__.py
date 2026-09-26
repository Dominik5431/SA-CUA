"""
Quarantined LLM (QLLM) analysis module for ucm_agent.

Ported from the Untrusted Content Masking (UCM) reference implementation
(https://arxiv.org/abs/2607.05277, src/agent/qllm/ in that repo). Provides
secure analysis of untrusted web content by routing it through a separate,
type-constrained LLM call instead of exposing the raw text to the main
screen-acting agent.
"""

from .qllm_tool import QLLMTool
from .qllm_executor import (
    QLLMExecutor,
    QLLM_MAX_ENUM_OPTIONS,
    build_quarantined_system_prompt,
    build_quarantined_user_prompt,
    parse_quarantined_response,
    validate_constraints,
    get_default_result_for_type,
)
from .qllm_claude import ClaudeQLLMCaller
from .qllm_openai import OpenAIQLLMCaller

__all__ = [
    "QLLMTool",
    "QLLMExecutor",
    "ClaudeQLLMCaller",
    "OpenAIQLLMCaller",
    "QLLM_MAX_ENUM_OPTIONS",
    "build_quarantined_system_prompt",
    "build_quarantined_user_prompt",
    "parse_quarantined_response",
    "validate_constraints",
    "get_default_result_for_type",
]
