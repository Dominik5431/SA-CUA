# Fides + OSWorld Runner

This directory contains the Fides-style defended OSWorld runner used by the
paper artifact. The root [README.md](../README.md) is the authoritative entry
point for setup and experiment context.

## Setup

Use the shared `uv` environment and install the Fides-specific requirements:

```bash
cd camel-prompt-injection-main
uv sync --all-groups
source ./.venv/bin/activate
uv pip install -e ../OSWorld
uv pip install -r ../fides/requirements.txt
cd ..
```

The examples below use `--env-file .env`; change that path if you keep your API
keys elsewhere. For UI-TARS and the OpenCUA local server, set `UITARS_API_KEY`
to the API key configured on the OpenAI-compatible VLM server.

## Quick Start

Run from the repository root:

```bash
uv run --env-file .env fides/run.py \
  --path_to_vm "/path/to/Ubuntu0.vmx" \
  --headless \
  --observation_type screenshot \
  --max_trajectory_length 5 \
  --max_steps 10 \
  --history_n 30 \
  --max_tokens 4096 \
  --model uitars \
  --port 15001 \
  --vlm_path "/path/or/name/of/UI-TARS" \
  --test_all_meta_path OSWorld/src/osworld/evaluation_examples/test_small.json \
  --test_config_base_dir OSWorld/src/osworld/evaluation_examples \
  --result_dir ./results_fides_run_1 \
  --system_defense_level 1 \
  --model_defense_level 0 \
  --p_llm openrouter:openai/gpt-5-mini \
  --q_llm openrouter:openai/gpt-5-mini
```

## Arguments

### Environment / VM

- `--path_to_vm`: path to the VMware `.vmx` file.
- `--headless`: run the VM headless.
- `--action_space`: action type, default `pyautogui`.
- `--observation_type`: system observation type, one of `screenshot`, `a11y_tree`, `screenshot_a11y_tree`, or `som`.
- `--observation_type_uitars`: observation type used when calling the UI-TARS/OpenCUA/Claude agent, default `screenshot`.
- `--screen_width`, `--screen_height`: VM resolution.
- `--sleep_after_execution`: sleep after each action.
- `--max_steps`: max action steps per task.

### Agent / Task

- `--max_trajectory_length`: max trajectories per task.
- `--test_config_base_dir`: base directory for OSWorld task configs.
- `--domain`: domain to run, default `all`.
- `--test_all_meta_path`: JSON defining the task set.
- `--result_dir`: output root directory.

### Model / LLM

- `--model`: main agent backend; choices are `uitars`, `opencua`, `claude-4-sonnet-20250514`, `claude-4-5-sonnet-20250929`, and `claude-sonnet-4-5-20250929`.
- `--vlm_path`: served model name/path for UI-TARS.
- `--model_type`, `--infer_mode`, `--prompt_style`: UI-TARS configuration knobs.
- `--input_swap`: use copy/paste for typing content.
- `--language`: prompt language.
- `--max_pixels`, `--min_pixels`: vision token limits.
- `--temperature`, `--top_p`, `--top_k`: sampling config.
- `--history_n`: action history length.
- `--callusr_tolerance`: tolerance for user-call errors.
- `--max_tokens`: max tokens for model calls.
- `--stop_token`: optional stop token.
- `--p_llm`: planner model.
- `--q_llm`: Q-LLM verifier model.
- `--q_llm_second_check`: optional second verifier.
- `--port`: local port for the VLM API server.
- `--api_provider`: Claude provider, one of `anthropic`, `bedrock`, or `vertex`.
- `--token_log_path`: optional per-task token-count file name.

### Defenses

- `--model_defense_level`: model defense level from 0 to 4. Levels 3 and above require `--q_llm_second_check`.
- `--system_defense_level`: system defense level from 0 to 2.
- `--pllm_exposure`: planner feedback level. `0` uses `loop_and_planner_no_exposure.py`; `1` uses `loop_and_planner.py`.

## Output Structure

Results are written under `--result_dir` in a nested tree that depends on model
configuration:

```text
<result_dir>/
└── <model>/
    └── <p_llm>+fides/
        └── modeldef_<i>_systdef_<j>/
            └── qllm1_<q_llm>_qllm2_<q_llm_second_check>/
                ├── chrome/<task_id>/result.txt
                ├── vs_code/<task_id>/result.txt
                └── ...
```

Use `scripts/show_result_fides.py` to create `all_result.json` for one nested
result directory, then `scripts/aggregate_fides_results.py` to compute pass@k
across multiple runs. `scripts/run_fides_pipeline.sh` wraps this workflow.
