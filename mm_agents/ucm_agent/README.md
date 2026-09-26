# ucm_agent

Untrusted Content Masking (UCM) defense, ported from
[the source UCM repo](https://arxiv.org/abs/2607.05277) into RedTeamCUA. See
`UCM_AGENT_INTEGRATION_PLAN.md` at the repo root for the full design
rationale and open risks; this file covers usage and current limitations.

## How it differs from FidesAgent/SA-Agent

Fides/SA gate a separate "read" tool for untrusted content, but the raw
untrusted text is still visible in the screenshot/a11y-tree the main
planner sees every turn (labeling there is provenance/taint-tracking, not a
rendering change). UCM masks the live in-VM browser DOM via CDP **before
every screenshot is captured** — the untrusted text is never present in the
pixels the model sees in the first place. When a task needs information
from a masked element, the agent calls a `quarantined_llm_analysis` tool
with an element id, a natural-language question, and a restricted return
type (`bool`/`int`/`float`/`enum`/`date`); a separate Quarantined Model
reads only that element's text and returns a type-constrained value that
cannot carry injected instructions back to the main agent.

## Usage

Like every other agent in this repo, `--model` (and `--ucm_model`/
`--ucm_qllm_model` below) is a `"<provider> | <model_name>"` string
(`mm_agents/prompt_agent/agent.py`, `mm_agents/sa_agent/llm.py`'s
`build_client`). Two provider families are supported:

- `anthropic | <model>` — native Anthropic tool-calling
  (`client.messages.create(tools=[...])`).
- `vllm | <model>` / `openai | <model>` / `openrouter | <model>` — OpenAI
  Chat Completions tool-calling. This is the same path
  `PromptAgent`/`mm_agents/sa_agent` use for a local vLLM server, so any
  model already served for another agent in this repo works here too — the
  repo-wide `--model` default is in fact `"vllm | Qwen/Qwen3.5-9B"`.

**With Anthropic:**

```bash
python run.py \
  --agent_type UCMAgent \
  --action_space pyautogui \
  --observation_type screenshot \
  --model "anthropic | claude-sonnet-4-6" \
  --provider_name docker \
  --test_all_meta_path ./evaluation_examples/test_all_reddit.json \
  --result_dir ./results
```

Requires `ANTHROPIC_API_KEY` in the environment.

**With a local vLLM server (e.g. Qwen/Qwen3.5-9B):**

```bash
# Launch vLLM the same way as for the other agents, but WITH tool-calling
# enabled -- UCMAgent needs the model to support function/tool calls to
# choose between the computer_use and quarantined_llm_analysis tools (a
# plain chat-completions model with no tool support will never call
# quarantined_llm_analysis and will just narrate actions in plain text
# instead of acting on them).
vllm serve Qwen/Qwen3.5-9B \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --port 8000

python run.py \
  --agent_type UCMAgent \
  --action_space pyautogui \
  --observation_type screenshot \
  --model "vllm | Qwen/Qwen3.5-9B" \
  --provider_name docker \
  --test_all_meta_path ./evaluation_examples/test_all_reddit.json \
  --result_dir ./results
```

Uses the same `VLLM_PORT` (default `8000`) / `VLLM_BASE_URL` (default
`http://localhost:$VLLM_PORT/v1`) / `VLLM_API_KEY` (default `"EMPTY"`) env
vars as `PromptAgent` and `mm_agents/sa_agent/llm.py` — no separate vLLM
server or config needed if you already have one running for another agent.

Relevant flags (see `UCMAgent options` in `run.py --help`):

- `--ucm_model` — `"<provider> | <model_name>"` for the main screen-acting/
  tool-calling agent. Defaults to `--model`.
- `--ucm_qllm_model` — `"<provider> | <model_name>"` for the Quarantined
  Model (Q-Model) that reads masked element content. **Defaults to
  `--ucm_model` (i.e. the same provider+model as the main agent)** — so
  running the main agent entirely against a local vLLM server doesn't
  silently require `ANTHROPIC_API_KEY` too, mirroring
  `mm_agents/fides/loop.py`'s `q_llm or p_llm` default. Set this separately
  if you want e.g. a vLLM main agent with an Anthropic Q-Model (or vice
  versa).
- `--ucm_allow_unsolvable` — lets the agent declare `TASK UNSOLVABLE` for
  tasks that need verbatim free-text extraction the QLLM tool structurally
  cannot provide.
- `--ucm_site {reddit,owncloud,rocketchat}` — force which per-site marker
  script to inject instead of auto-detecting the platform from the current
  URL (`browser_guard.detect_site`).

Only `--action_space pyautogui` is supported (the repo-wide assertion in
`run.py`'s `test()` already restricts this for every agent type).

### A note on open-weight models and tool-calling reliability

`agent.py`'s `_call_openai_compatible` takes only the *first* tool call in
the response and otherwise treats a tool-call-free response as plain text
(checked against the `TASK COMPLETE`/`TASK UNSOLVABLE` markers, same as the
Anthropic path). Smaller open-weight models are less reliable at strict
tool-call schema adherence than Claude — if the model narrates an action in
prose instead of calling `computer_use`, `UCMLoop.loop()`'s "no tool call
and not a completion signal" branch nudges it once before ending the
episode (see `loop.py`). This hasn't been tuned against Qwen3.5-9B
specifically; treat it as a starting point, not a validated configuration.

## What's genuinely new here vs. ported

- **New**: `browser_guard.py` (CDP masking + live HTML retrieval — this
  repo already exposes Chrome's remote-debugging port and already uses
  Playwright's `connect_over_cdp` elsewhere, so this needed no VNC/xdotool
  hacks unlike the source repo), `agent.py`/`loop.py` (this repo's
  agent-vs-env-owning-loop split, plus the multi-provider
  Anthropic/OpenAI-compatible client + canonical-message serialization —
  the source UCM repo only ever talked to Anthropic), `actions.py`
  (action-dict -> pyautogui string), `qllm/qllm_openai.py` (an
  OpenAI-compatible Q-Model caller alongside the ported `qllm_claude.py`),
  and the three per-site marker scripts.
- **Ported near-verbatim** (pure logic, no environment dependency):
  `qllm/qllm_executor.py`, `qllm/qllm_tool.py`'s Anthropic tool format,
  `qllm/qllm_claude.py` (network logging to the source repo's demo-site log
  endpoints removed — not applicable here), and the QLLM-tool documentation
  blocks in `prompts.py`.

## Known limitations (not yet live-verified)

1. **The three per-site marker scripts
   (`js/{reddit,owncloud,rocketchat}_marker.js`) are best-effort, written
   from general knowledge of each platform's typical markup, not from
   inspecting the actual running containers.** Every selector needs to be
   checked against real rendered HTML before this is trusted for an
   evaluation run. The recommended way to firm these up: run the source
   UCM repo's `src/automatic_boundary_detection/` pipeline against a page
   snapshot of each of these three sites (it already supports "add a new
   site" — see that repo's README) to get an LLM-proposed selector list,
   then hand-verify against the live container.
2. **"Which tab is active" is a heuristic** (`BrowserGuard.active_page()`
   picks the most recently created non-blank page in the CDP browser
   context) — fine for the current single-tab-per-task shape of the adv/
   platforms, but not a verified foreground-window check. Revisit if a task
   ever needs multiple simultaneous tabs.
3. **`observation_type=screenshot` only.** The marker scripts strip
   `title`/`aria-label` on masked elements (so hover tooltips can't leak
   content), but the AT-SPI accessibility-tree text nodes for masked
   elements have not been checked to confirm they read back as the
   placeholder rather than the underlying DOM text. Don't pass
   `--observation_type a11y_tree`/`screenshot_a11y_tree` until that's
   verified.
4. **No offline test exercises the actual CDP connection or the marker
   scripts against a live DOM** — `test/ucm_offline.py` covers the portable
   logic (action-string building, QLLM element resolution/parsing, site
   detection, prompt assembly, multi-provider client/message wiring) with
   everything else mocked out. A live spike against a running container is
   the next validation step (see `UCM_AGENT_INTEGRATION_PLAN.md` §9).
5. **The `vllm`/`openai`/`openrouter` path has not been exercised against a
   live server** — only client construction and message serialization are
   covered offline (no network calls happen at construction time). A real
   run against a served Qwen3.5-9B (or any other tool-calling-enabled vLLM
   model) is the next validation step, and is also where tool-calling
   reliability (see the "open-weight models" note above) would actually
   surface.
