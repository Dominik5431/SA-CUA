# llm_rules_container_v17_all

A COPY of `llm_rules_container_v17_suites_or/` + `llm_rules_container_v17_web_or/`, nothing else.
It exists because `rule_acquisition.py --rules_dir` and `ucm_baseline/compare.py --rules` each
take ONE directory, and the v17 arm was proposed in two runs:

  llm_rules_container_v17_suites_or   45 sets, run v17-suites-or-20260914T075734Z
  llm_rules_container_v17_web_or      90 sets, run v17-web-or-20260920T213125Z

Same model (openai/gpt-5.6-luna), same pipeline, same flags -- the second run's settings were
taken from the first run's recorded meta, so the union is one arm and not a mixture. The two
source directories stay authoritative; regenerate this one rather than editing it.
