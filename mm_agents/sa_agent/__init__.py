"""
SA-Agent — a self-contained fork of mm_agents/fides extended with information-flow
control: taint labels sourced from mm_agents/accessibility_tree_wrap, per-value
label propagation through the P-LLM's variable store, deterministic policy gating
of consequential tool calls (see policies.py), and verification-based
endorsement (see endorse.py).

Entry point: mm_agents.sa_agent.loop.SAAgentLoop (imported directly by run.py;
nothing is re-exported here to keep package import light).

DO NOT ADD IMPORTS TO THIS FILE. It is load-bearing, not merely tidy.

at-spi/extraction.py imports mm_agents.sa_agent.screen_labels, and three at-spi labelers import
mm_agents.sa_agent.llm, so the dependency already runs at-spi -> sa_agent. masking.py closes
the loop the other way (sa_agent -> at-spi), which stays a DIAMOND rather than a cycle purely
because importing this package executes nothing. A single convenience re-export here makes
`import mm_agents.sa_agent.screen_labels` re-enter through masking.py mid-initialisation, and
the traceback will point at extraction.py rather than at the import that caused it.
"""
