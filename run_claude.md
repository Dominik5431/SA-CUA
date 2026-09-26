# Running the Claude CUA agent

## Constructor arguments

| Argument | Default | Mapped from CLI | What it does |
| --- | --- | --- | --- |
| `platform` | `"Ubuntu"` | *(hardcoded)* | OS platform passed to `AnthropicAgent`. `run.py` always runs the VM as Ubuntu. |
| `model` | `"claude-sonnet-5"` | `--model` | Anthropic model name used for the computer-use loop. |
| `provider` | `"anthropic"` | `--api_provider` | Backend to reach the model. Only `anthropic` is enabled in `_PROVIDER_MAP` (Bedrock/Vertex are commented out). |
| `max_tokens` | `4096` | `--max_tokens` | Maximum completion tokens per model call. Note: `run.py`'s global default is `8192`, overriding the constructor's `4096` default. |
| `api_key` | `None` | *(environment)* | Anthropic API key. If `None`, falls back to `os.environ["ANTHROPIC_API_KEY"]`. `run.py` does not pass it, so set `ANTHROPIC_API_KEY` (e.g. in `.env`, which `run.py` auto-loads). |
| `system_prompt_suffix` | `""` | *(not wired to CLI)* | Extra text appended to the system prompt. Not exposed by `run.py`. |
| `only_n_most_recent_images` | `10` | *(not wired to CLI)* | How many of the most recent screenshots are kept in the message history sent to the model. Not exposed by `run.py`. |
| `action_space` | `"claude_computer_use"` | `--action_space` | Action space the agent emits. Use `claude_computer_use` for the Claude CUA tools. |
| `screen_width` | `1920` | `--screen_width` | Virtual screen width in pixels (part of `screen_size` passed to `AnthropicAgent` and `DesktopEnv`). |
| `screen_height` | `1080` | `--screen_height` | Virtual screen height in pixels (part of `screen_size`). |
| `no_thinking` | `False` | `--no_thinking` | Disables Claude extended thinking when set. |
| `use_isp` | `False` | `--use_isp` | Enables interleaved scratchpad mode (legacy Claude 4.5/4.6 models). |
| `effort` | `"max"` | `--effort` | Adaptive thinking effort for Claude 4.7+ models: `low`, `medium`, `high`, or `max`. |
| `temperature` | `None` | `--temperature` | Sampling temperature passed to the Anthropic client. `None` leaves it unset (Anthropic default). |
| `top_p` | `None` | `--top_p` | Nucleus sampling parameter. `None` leaves it unset (Anthropic default). |
| `max_steps` | `15` | *(loop, from `--max_steps`)* | Maximum agent steps per task. In `run.py` this comes from `--max_steps` (global default `50`, but the launch config uses `10`). |

### Other `run.py` flags used in the launch config (not constructor args)

| Flag | Value in launch config | What it does |
| --- | --- | --- |
| `--headless` | *(set)* | Run the VM in headless mode (no visible display). |
| `--provider_name` | `docker` | Environment provider that hosts the VM. |
| `--observation_type` | `screenshot` | Only screenshot works with Claude CUA agent. |
| `--test_all_meta_path` | `evaluation_examples/test_one.json` | JSON listing the task(s) to run. |
| `--test_config_base_dir` | `evaluation_examples` | Base directory for task configuration files. |
| `--result_dir` | `experiment_results/ClaudeSonnet5_testing` | Where run results are written. |

## Bash command (equivalent to the VSCode launch config)

Run from the repository root:

```bash
python run.py \
  --headless \
  --provider_name docker \
  --agent_type ClaudeCUAAgent \
  --model claude-sonnet-5 \
  --api_provider anthropic \
  --action_space claude_computer_use \
  --observation_type screenshot \
  --max_steps 10 \
  --test_all_meta_path evaluation_examples/test_all_reddit.json \
  --test_config_base_dir evaluation_examples \
  --result_dir experiment_results/ClaudeSonnet5_testing
```

Before running, make sure the Anthropic API key is available. `run.py` loads
`.env` from the repository root automatically, so one of these works:

```bash
# option 1: .env file
#   ANTHROPIC_API_KEY=sk-ant-...

# option 2: shell export
export ANTHROPIC_API_KEY=sk-ant-...
```
