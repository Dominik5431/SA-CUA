"""
ucm_agent — Untrusted Content Masking (UCM) defense for RedTeamCUA.

Ports the UCM defense (https://arxiv.org/abs/2607.05277) into this repo's
mm_agents/ scaffold. See UCM_AGENT_INTEGRATION_PLAN.md at the repo root for
the full design rationale; mm_agents/ucm_agent/README.md for usage and
known limitations.
"""

from mm_agents.ucm_agent.agent import UCMAgent
from mm_agents.ucm_agent.loop import UCMLoop

__all__ = ["UCMAgent", "UCMLoop"]
