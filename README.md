# SA-CUA

![SA-CUA Overview](assets/figure.png)


---
This is the repository of SA-CUA, a dual-LLM framework that generates a reliable
trust boundary for CUAs (Computer-Use Agents) by deriving conservative deterministic rules from the accessibility tree for unmasking trusted regions from the privileged LLM’s screenshot, keeping trusted and untrusted content separated. Human interaction is only required for proper task specification in the beginning. During the task a robust
endorsement channel allows to pass from the untrusted to the trusted domain by
verifying information via trusted channels. This allows the agent to autonomously
handle tasks involving untrusted information. SA-CUA achieves a much higher
utility than competing methods while keeping the attack success rate at zero.

## What's here

### Defended agents

All agents are run through a single entry point, `run.py`, selected with `--agent_type`. The
defended agents and the baseline they are measured against:

| Agent | Defense strategy |
|---|---|
| `PromptAgent` | Baseline. A standard prompt-based CUA with **no** defense. |
| `SA-Agent` | Capability-scoped dual-LLM agent. A trusted planner (P-LLM) that never observes raw screen content delegates capability-scoped sub-tasks to a quarantined executor (Q-VLM). Adds zone-based screen taint labels, a pre-execution policy gate, a typed endorsement channel, and hybrid web/terminal tools. |
| `FidesAgent` | Fides-style defense: the `no_exposure` variant redacts tool outputs into variable references so the planner sees tokens, not content, plus a cross-model consistency check. |
| `CamelAgent` | The [CaMeLCUA](https://arxiv.org/abs/2601.09923) pipeline: control-flow integrity via isolation between a planner P-LLM  and a Q-VLM. |

### Endorsement benchmark

`endorsement/` is a small, **offline, deterministic** benchmark for the SA-Agent
endorsement channel. It asks a narrower question than the full episodes: *given an answer from
an untrusted source, does the channel reach the right trust verdict, for the right reason, and adopt
the right value?* It runs with no network and no model calls, and imports the agent's real code
rather than a copy of it. See [`endorsement/README.md`](endorsement/README.md).

### Evaluation suites

`evaluation_examples/` holds the generated episode configs used for evaluation, combining:

- **RTC-Bench** core and extended indirect-prompt-injection tasks (Reddit/Forum, OwnCloud, RocketChat).
- The **WASP** GitLab and Reddit suites (GitLab injection goes through the REST API rather than raw SQL).
- **Custom malicious** tasks (Reddit and Thunderbird) that attempt to bypass the defense mechanisms by trying to substitute a value the agent plugs into an otherwise benign command. All custom malicious tasks are listed in [`evaluation_examples/test_all_custom_malicious_tasks.json`](evaluation_examples/test_all_custom_malicious_tasks.json).
- The **UCM utility suite** (`ucm_sites/`, 10 self-hosted sites) that measures whether masking *breaks benign utility* rather than whether the agent gets hijacked.
- OSWorld examples integrated from the OSWorld benchmark, listed in [`evaluation_examples/osworld_test_all.json`](evaluation_examples/osworld_test_all.json).

---

## Setup

### 1. Python environment

Python **3.12** is required (see `pyproject.toml`). The recommended path is `uv`:

```bash
uv sync
source .venv/bin/activate
```

### 1b. Example data

The OSWorld example-data archives are distributed separately rather than as a git submodule, because
the anonymized host serves files over HTTP but not over the git protocol. Fetch and unpack them into
the path the generators expect:

```bash
mkdir -p submodules/OSWorld_examples_data
curl -L https://anonymous.4open.science/api/repo/OSWorld_examples_data/zip -o /tmp/osworld_examples_data.zip
unzip /tmp/osworld_examples_data.zip -d submodules/OSWorld_examples_data
```

Only the generators under `evaluation_examples/generate_thunderbird_examples/` read these archives.
The task configs they produce are already committed, so evaluation runs do **not** need this step.

### 1c. Optional baselines

Two comparison baselines import code from checkouts this repository does not ship. Place them at the
paths below to enable them; everything else — SA-Agent, the prompt agents, and every evaluation
suite — runs without them.

| Baseline | Expected location | Enables |
| --- | --- | --- |
| CaMeL (`--agent_type CamelAgent`) | `submodules/camel-cua/camel-prompt-injection-main/` | the CaMeL rows in `results.py` |
| UCM masking (`at-spi/ucm_baseline/`, the `ucm` metric) | `$UCM_REPO`, default `../untrusted-content-masking` | the UCM baseline comparison |

### 2. Hybrid sandbox

Episodes run in a hybrid environment: an **OSWorld** Ubuntu VM (the OS side) plus **Docker-hosted web
replicas** (the web side). Only the `docker` provider is supported:

- `docker` (default) — the OS runs in a container/VM image and the web platforms are Docker
  containers, exposed to the agent behind plausible port-less hostnames (`reddit.com`, `gitlab.com`,
  `owncloud.com`, `rocketchat.com`) through an nginx `rtc-web-proxy`. This is the mode used for
  parallel evaluations.

The web platforms come from [WebArena](https://web-arena-x.github.io/webarena/) (the Forum/Reddit
replica) and [TheAgentCompany](https://the-agent-company.com/) (OwnCloud, RocketChat), with a GitLab
replica added for the WASP suite and a set of ten self-hosted sites for the UCM utility suite.

### 3. Environment variables

Model credentials are read from the environment (or loaded automatically from `.env`
by `run.py`). The relevant variables depend on which provider/agent you use:

```bash
# Local vLLM (default backend: vllm | Qwen/Qwen3.5-9B)
export VLLM_PORT='8000'            # optional, default 8000
export VLLM_BASE_URL='http://localhost:8000/v1'   # optional

# OpenRouter
export OPENROUTER_API_KEY='<YOUR_OPENROUTER_KEY>'
# if using openrouter

# GitLab WASP suite (admin token with api + sudo scopes; see adv/WebArena_Gitlab/INJECTION_THREAT_MODEL.md)
export GITLAB_ADMIN_TOKEN='<token>'
```

Model flags throughout the repo use the `"<provider> | <model_name>"` convention (e.g.
`"openrouter | openai/gpt5.6-luna"`, `"vllm | Qwen/Qwen3.5-9B"`); a few agent-specific flags also
accept a `"provider:model_name"` spelling.

---

## Running an evaluation

Everything goes through `run.py`. The general shape:

```bash
python run.py \
  --agent_type <Agent> \
  --provider_name docker \
  --observation_type screenshot \
  --action_space pyautogui \
  --model "<provider> | <model_name>" \
  --test_all_meta_path evaluation_examples/<suite>.json \
  --test_config_base_dir evaluation_examples \
  --result_dir ./results
```

`--test_all_meta_path` selects the episode set; `--test_config_base_dir` is the root under which
the per-episode configs referenced by that meta file live. Result output follows a nested
`<result_dir>/<model|run_label>/...` layout, and each episode writes `result.txt` (benign/utility)
and `adversary_result.txt` (injection success).

### Baseline (`PromptAgent`)

```bash
python run.py \
    --headless \
    --provider_name docker \
    --agent_type PromptAgent \
    --observation_type screenshot \
    --model "vllm | Qwen/Qwen3.8-27B" \
    --vllm_port 8002 \
    --test_all_meta_path evaluation_examples/osworld_test_all.json \
    --result_dir ./results \
    --max_steps 50 \
    --max_tokens 8192 \
```

### `SA-Agent`

```bash
python run.py \
    --headless \
    --provider_name docker \
    --agent_type SA-Agent \
    --observation_type screenshot_a11y_tree \
    --action_space pyautogui \
    --model "vllm | Qwen/Qwen3.8-27B" \
    --vllm_port "8000" \
    --test_all_meta_path evaluation_examples/test_all_reddit_fixed.json \
    --task_filter reddit_1 \
    --result_dir ./results \
    --max_steps 50 \
    --max_tokens 8192 \

```

### `CamelAgent`

The CaMeL adapter requires an explicit model triplet (see `run_camel.sh` for a complete example):

```bash
python run.py \
  --headless \
  --provider_name docker \
  --agent_type CamelAgent \
  --observation_type screenshot_a11y_tree \
  --action_space pyautogui \
  --camel_p_llm_model "openrouter | openai/gpt-5.6-luna" \
  --camel_q_llm_model "vllm | Qwen/Qwen3.8-27B" \
  --vllm_port "8002" \
  --max_steps 50 \
  --max_tokens 12000 \
  --test_all_meta_path evaluation_examples/test_all_rtc_hard.json \
  --result_dir ./results \
```

### Endorsement benchmark

```bash
python -m endorsement.harness.runner               # full run, no network, no model
python -m endorsement.harness.runner -v --scenario repo_url_crosscheck
python -m endorsement.harness.test_harness         # self-tests for the harness itself
python -m endorsement.harness.test_locate          # unit checks for the screen primitives
```

---

## Repository layout

```
run.py                        main evaluation entry point (all agents)
mm_agents/                    agent implementations
  sa_agent/                 SA-Agent: dual-LLM IFC, taint labels, policy gate, endorsement
  fides/                      FidesAgent
  camel_agent/                CaMeL pipeline adapter
  ucm_agent/                  Untrusted Content Masking agent
  prompt_agent/               baseline PromptAgent
endorsement/             offline, deterministic benchmark for the SA endorsement channel
evaluation_examples/          generated example configs (RTC-Bench, WASP, UCM utility, custom_malicious, OSWorld)
adv/                          per-platform adversarial injection scripts (Reddit, OwnCloud, RocketChat, GitLab)
desktop_env/                  Modified environment from RedTeamCUA
ucm_sites/                    UCM utility suite: 10 self-hosted sites + scoring
at-spi/                       AT-SPI accessibility-tree captures used for screen labeling / masking
submodules/                   not shipped; populated by the user (see Setup 1b / 1c)
   OSWorld_examples_data/     example-data archives for the Thunderbird generators
   camel-cua/                 CaMeL pipeline, for the optional CamelAgent baseline
```

---

## Attribution and license

SA-CUA is built on top of **RedTeamCUA** (the hybrid sandbox and RTC-Bench), which is itself
developed on the OSWorld framework; the web platforms come from WebArena and TheAgentCompany. The
defense agents port or adapt the following published defenses: 
the Fides defense ([arXiv:2505.23643](https://arxiv.org/pdf/2505.23643)), Untrusted Content Masking ([arXiv:2607.05277](https://arxiv.org/abs/2607.05277)),
and the CaMeL CUA architecture *"CaMeLs Can Use Computers Too"* ([arXiv:2601.09923](https://arxiv.org/abs/2601.09923)).

This project is licensed under the Apache License 2.0 (see [`LICENSE`](LICENSE)). Upstream licenses:

- [RedTeamCUA / OSWorld License (Apache 2.0)](https://github.com/xlang-ai/OSWorld/blob/main/LICENSE)
- [WebArena License (Apache 2.0)](https://github.com/web-arena-x/webarena/blob/main/LICENSE)
- [TheAgentCompany License (MIT)](https://github.com/TheAgentCompany/TheAgentCompany/blob/main/LICENSE)

