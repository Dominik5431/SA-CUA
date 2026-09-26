"""Script to run end-to-end evaluation on the benchmark. Including the VM, and the general loop. 
"""

import argparse
import datetime
import json
import logging
import os
import sys

import inspect
import types
from typing import Any

from tqdm import tqdm

from claude import FidesAnthropicAgent
from osworld.mm_agents.anthropic.utils import APIProvider
from lattice import IntegrityLabel, InverseLattice, PowersetLattice, ProductLabel

from prompts import SYST_PROMPT_PLLM_FULL_EXPOSURE, SYST_PROMPT_PLLM_NO_EXPOSURE, build_pllm_prompt, SYST_DEF_1, SYST_DEF_2, SYST_DEF_3
from tool_scheme import Tool
import main_loop
from desktop_env.desktop_env import DesktopEnv
from mm_agents import MAX_IMAGE_HISTORY_LENGTH, openrouter
from uitars import FidesUITARSAgent
from opencua import FidesOpenCUAAgent
from qllm import QLLMAgent
import policies as policy_modules

from tools import *


logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
os.makedirs("logs", exist_ok=True)

file_handler = logging.FileHandler(
    os.path.join("logs", "normal-{:}.log".format(datetime_str)), encoding="utf-8"
)
debug_handler = logging.FileHandler(
    os.path.join("logs", "debug-{:}.log".format(datetime_str)), encoding="utf-8"
)
stdout_handler = logging.StreamHandler(sys.stdout)
sdebug_handler = logging.FileHandler(
    os.path.join("logs", "sdebug-{:}.log".format(datetime_str)), encoding="utf-8"
)

file_handler.setLevel(logging.INFO)
debug_handler.setLevel(logging.DEBUG)
stdout_handler.setLevel(logging.INFO)
sdebug_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    fmt="\x1b[1;33m[%(asctime)s \x1b[31m%(levelname)s \x1b[32m%(module)s/%(lineno)d-%(processName)s\x1b[1;33m] \x1b[0m%(message)s"
)
file_handler.setFormatter(formatter)
debug_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)
sdebug_handler.setFormatter(formatter)

stdout_handler.addFilter(logging.Filter("desktopenv"))
sdebug_handler.addFilter(logging.Filter("desktopenv"))

logger.addHandler(file_handler)
logger.addHandler(debug_handler)
logger.addHandler(stdout_handler)
logger.addHandler(sdebug_handler)
logger.propagate = False

logger = logging.getLogger("desktopenv.experiment")
logger.propagate = False

def config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end evaluation on the benchmark"
    )

    parser.add_argument("--path_to_vm", type=str, default=None)
    parser.add_argument(
        "--headless", action="store_true", help="Run in headless machine"
    )
    parser.add_argument(
        "--action_space", type=str, default="pyautogui", help="Action type"
    )
    parser.add_argument(
        "--observation_type",
        choices=["screenshot", "a11y_tree", "screenshot_a11y_tree", "som"],
        default="screenshot_a11y_tree",
        help="Observation type for the system",
    )
    parser.add_argument(
        "--observation_type_uitars",
        choices=["screenshot", "a11y_tree", "screenshot_a11y_tree", "som"],
        default="screenshot",
        help="Observation type only when calling the uitars agent",
    )
    parser.add_argument("--screen_width", type=int, default=1920)
    parser.add_argument("--screen_height", type=int, default=1080)
    parser.add_argument("--sleep_after_execution", type=float, default=0.0)
    parser.add_argument("--max_steps", type=int, default=15)

    parser.add_argument("--max_trajectory_length", type=int, default=5)
    parser.add_argument(
        "--test_config_base_dir", type=str, default="evaluation_examples"
    )

    parser.add_argument("--model", type=str, default="Qwen/Qwen3.5-9B", choices=["uitars", "opencua", "claude-4-sonnet-20250514", "claude-4-5-sonnet-20250929", "claude-sonnet-4-5-20250929"])
    parser.add_argument("--vlm_path", type=str, default=None)
    parser.add_argument("--model_type", type=str, default="qwen25vl")
    parser.add_argument("--infer_mode", type=str, default="qwen25vl_normal")
    parser.add_argument("--prompt_style", type=str, default="qwen25vl_normal")
    parser.add_argument("--input_swap", action="store_true", help="Use copy and paste to type content")
    parser.add_argument("--language", type=str, default="English")
    parser.add_argument("--max_pixels", type=float, default=16384*28*28)
    parser.add_argument("--min_pixels", type=float, default=100*28*28)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--top_k", type=int, default=-1)
    parser.add_argument("--history_n", type=int, default=MAX_IMAGE_HISTORY_LENGTH,
                        help="Screenshots the screen VLM keeps in context before the "
                             "window slides (mm_agents.MAX_IMAGE_HISTORY_LENGTH).")
    parser.add_argument("--callusr_tolerance", type=int, default=3)
    parser.add_argument("--max_tokens", type=int, default=4096)
    parser.add_argument("--stop_token", type=str, default=None)

    parser.add_argument("--p_llm", type=str, default="gpt-4o")

    parser.add_argument("--domain", type=str, default="all")
    parser.add_argument(
        "--test_all_meta_path", type=str, default="evaluation_examples/test_small.json"
    )

    parser.add_argument("--result_dir", type=str, default="./results")


    parser.add_argument("--model_defense_level", type=int, default=0, help="Model defense level from 0 to 4")
    parser.add_argument("--system_defense_level", type=int, default=0, help="System defense level from 0 to 2")
    parser.add_argument("--pllm_exposure", type=int, default=0, help="""        
                        pllm_exposure_level: controls the level of feedback provided to the planner at each iteration. 
                        Level 0: no feedback is provided between iteration (original fides, all variables are redacted). 
                        Level 1: provides the output of the summarize_screenshot_content function."""
                    )

    parser.add_argument("--q_llm", type=str, default=None, help="The model used for the qllm agent")
    parser.add_argument("--q_llm_second_check", type=str, default=None, help="The model used for the qllm agent second check")

    parser.add_argument("--port", type=int, default=15003, help="The local port for the VLM API server")

    parser.add_argument("--api_provider", type=str, default="anthropic", 
                        choices=["anthropic", "bedrock", "vertex"],
                        help="Anthropic API provider: anthropic (direct API), bedrock (AWS), or vertex (GCP)")
    
    parser.add_argument("--token_log_path", type=str, default=None, help="File to log token counts when querying the qllm")
    args = parser.parse_args()

    openrouter.configure(args.max_trajectory_length)

    return args


def parse_q_llm(q):
    """Safely extract model name from q_llm string."""
    if not q:
        return "None"
    try:
        parts = q.split(':')
        if len(parts) < 2:
            return q
        right = parts[1]
        if '/' in right:
            return right.split('/')[1]
        return right
    except Exception:
        return "None"


def test(args: argparse.Namespace, test_all_meta: dict) -> None:
    scores = []
    max_steps = args.max_steps

    if args.observation_type == "screenshot":
        assert args.observation_type_uitars not in ["a11y_tree", "screenshot_a11y_tree", "som"]
    if args.pllm_exposure == 0:
        from loop_and_planner_no_exposure import LabeledBasicPlanner, LabeledPlanner, LabeledPlanningLoop
    else:
        from fides.loop_and_planner import LabeledBasicPlanner, LabeledPlanner, LabeledPlanningLoop

    logger.info("Args: %s", args)

    python_tools: list[Any] = [
        run_vlm,
        find,
        summarize_screenshot_content,
        checkdone,
        move_to,    
        click,
        mouse_down,
        mouse_up,
        right_click,
        hscroll,
        vscroll,
        type_text,
        key_down,
        key_up,
        hotkey,
        get_page_elements,
        get_page_text,
        find_element_by_text,
        verify_hypothesis,
    ]
    if args.system_defense_level >=1:
        python_tools.remove(run_vlm)
    if args.system_defense_level >=2:
        python_tools.remove(summarize_screenshot_content)
        python_tools.remove(get_page_elements)
        python_tools.remove(get_page_text)
        python_tools.remove(find_element_by_text)
        python_tools.remove(verify_hypothesis)
    tools: list[Tool] = [ Tool.from_callable(t) for t in python_tools ]

    policies = [
        func
        for name, func in globals().items()
        if isinstance(func, types.FunctionType)
        and inspect.getmodule(func) == policy_modules
    ]

    universe = frozenset({})
    initial_label = ProductLabel(IntegrityLabel.trusted(), InverseLattice(PowersetLattice(subset=universe, universe=universe)))

    qllm1_name = parse_q_llm(args.q_llm)
    qllm2_name = parse_q_llm(args.q_llm_second_check)
    p_llm_name = parse_q_llm(args.p_llm)
    model_dir = (
        "uitars" if args.model == "uitars" else "opencua" if args.model == "opencua" else "claude" if "claude" in args.model else "unknown"
    )
    parent_result_dir = os.path.join(
        args.result_dir,
        model_dir,
        f"{p_llm_name}+fides",
        f"modeldef_{args.model_defense_level}_systdef_{args.system_defense_level}",
        f"qllm1_{qllm1_name}_qllm2_{qllm2_name}",
    )

    logger.info("Environment initialized.")
    for domain in tqdm(test_all_meta, desc="Domain"):
        for example_id in tqdm(test_all_meta[domain], desc="Example", leave=False):
            logger.info(f"Running example {example_id} in domain {domain}")
            config_file = os.path.join(
                args.test_config_base_dir, f"examples/{domain}/{example_id}.json"
            )
            with open(config_file, "r", encoding="utf-8") as f:
                example = json.load(f)

            logger.info(f"[Domain]: {domain}")
            logger.info(f"[Example ID]: {example_id}")

            instruction = example["instruction"]
 
            logger.info(f"[Instruction]: {instruction}")

            example_result_dir = os.path.join(
                parent_result_dir,
                domain,
                example_id,
            )
            os.makedirs(example_result_dir, exist_ok=True)

            if args.token_log_path is not None:
                token_log_path = os.path.join(example_result_dir, args.token_log_path)
                os.makedirs(os.path.dirname(token_log_path), exist_ok=True)
                with open(token_log_path, "w") as f:
                    f.write("")
            else:
                token_log_path = None

            if args.model == "uitars":
                agent = FidesUITARSAgent(
                    action_space=args.action_space,
                    observation_type=args.observation_type_uitars,
                    max_trajectory_length=args.max_trajectory_length,
                    model_type=args.model_type,
                    port=args.port,
                    vlm_path=args.vlm_path,
                    token_log_path=token_log_path,
                    runtime_conf = {
                        "infer_mode": args.infer_mode,
                        "prompt_style": args.prompt_style,
                        "input_swap": args.input_swap,
                        "language": args.language,
                        "history_n": args.history_n,
                        "max_pixels": args.max_pixels,
                        "min_pixels": args.min_pixels,
                        "callusr_tolerance": args.callusr_tolerance,
                        "temperature": args.temperature,
                        "top_p": args.top_p,
                        "top_k": args.top_k,
                        "max_tokens": args.max_tokens
                    }
                )
            elif args.model == "opencua":
                coordinate_type = "absolute" if args.observation_type_uitars == "screenshot_a11y_tree" else "relative"
                agent = FidesOpenCUAAgent(
                    action_space=args.action_space,
                    observation_type=args.observation_type_uitars,
                    max_image_history_length=args.history_n,
                    model_type=args.model_type,
                    port=args.port,
                    model=args.model,
                    history_type="action_history",
                    coordinate_type=coordinate_type,
                    max_tokens=8192,
                )
            elif "claude" in args.model:

                provider_map = {
                    "anthropic": APIProvider.ANTHROPIC,
                    "bedrock": APIProvider.BEDROCK,
                    "vertex": APIProvider.VERTEX,
                }
                agent = FidesAnthropicAgent(
                    model=args.model,
                    provider=provider_map[args.api_provider],
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    only_n_most_recent_images=args.history_n,
                    action_space=args.action_space,
                    observation_type=args.observation_type_uitars,
                    max_trajectory_length=args.max_trajectory_length,
                    screen_width=args.screen_width,
                    screen_height=args.screen_height,
                )
            elif args.model.startswith("Qwen"):
                coordinate_type = "absolute" if args.observation_type_uitars == "screenshot_a11y_tree" else "relative"
                agent = FidesOpenCUAAgent(
                    action_space=args.action_space,
                    observation_type=args.observation_type_uitars,
                    max_image_history_length=args.history_n,
                    model_type=args.model_type,
                    port=args.port,
                    model=args.model,
                    history_type="action_history",
                    coordinate_type=coordinate_type,
                    max_tokens=8192,
                )
            else:
                raise ValueError(f"Unknown model type: {args.model}")
            

            qllm_agent = QLLMAgent(
                action_space=args.action_space,
                observation_type="a11y_tree",
                max_trajectory_length=args.max_trajectory_length,
                model=args.q_llm,
                token_log_path=token_log_path
            )

            if args.q_llm_second_check:
                qllm_second_check_agent = QLLMAgent(
                    action_space=args.action_space,
                    observation_type="a11y_tree",
                    max_trajectory_length=args.max_trajectory_length,
                    model=args.q_llm_second_check,
                    token_log_path=token_log_path
                )


            env = DesktopEnv(
                path_to_vm=args.path_to_vm,
                action_space=agent.action_space,
                screen_size=(args.screen_width, args.screen_height),
                headless=args.headless,
                os_type = "Ubuntu",
                require_a11y_tree="screenshot_a11y_tree",
            )

            functions_block = SYST_DEF_1 if args.system_defense_level == 0 else SYST_DEF_2 if args.system_defense_level == 1 else SYST_DEF_3
            exposure_block = SYST_PROMPT_PLLM_NO_EXPOSURE if args.pllm_exposure == 0 else SYST_PROMPT_PLLM_FULL_EXPOSURE
            system_prompt = build_pllm_prompt(
                functions_block=functions_block,
                exposure_block=exposure_block
            )
            planner = LabeledBasicPlanner(
                tools=tools,
                state=[{'role': 'system', 'content': f'{system_prompt}'}],
                model=args.p_llm,
            )

            loop = LabeledPlanningLoop(
                planner=planner,
                model=args.p_llm,
                tools=tools,
                policy=policies,
                qllm_agent=qllm_agent,
                qllm_agent_second=qllm_second_check_agent if args.q_llm_second_check else None,
                agent=agent,
                env=env,
                example_result_dir=example_result_dir,
                step_idx=0,
                max_steps=max_steps,
                observation_type=args.observation_type,
                model_defense_level=args.model_defense_level,
                token_log_path=token_log_path,
            )
            
            try:
                main_loop.run_single_example(
                    loop,
                    initial_label,
                    example,
                    instruction,
                    example_result_dir,
                    scores,
                )

            except Exception as e:

                logger.error(f"Exception in {domain}/{example_id}: {e}")
                with open(f"{example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
                    json.dump([msg for msg in loop.planner.history], f, indent=2)
                env.controller.end_recording(
                    os.path.join(example_result_dir, "recording.mp4")
                )
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                    f.write(
                        json.dumps(
                            {"Error": f"Time limit exceeded in {domain}/{example_id}"}
                        )
                    )
                    f.write("\n")
                    f.write(json.dumps({
                        "Error": f"{e.__class__.__name__} in {domain}/{example_id}: {e}"
                    }))
                    f.write("\n")

    env.close()
    logger.info(f"Average score: {sum(scores) / len(scores)}")


def get_unfinished(
    action_space, use_model, observation_type, result_dir, total_file_json
):
    target_dir = os.path.join(result_dir, action_space, observation_type, use_model)

    if not os.path.exists(target_dir):
        return total_file_json

    finished = {}
    for domain in os.listdir(target_dir):
        finished[domain] = []
        domain_path = os.path.join(target_dir, domain)
        if os.path.isdir(domain_path):
            for example_id in os.listdir(domain_path):
                if example_id == "onboard":
                    continue
                example_path = os.path.join(domain_path, example_id)
                if os.path.isdir(example_path):
                    if "result.txt" not in os.listdir(example_path):
                        for file in os.listdir(example_path):
                            os.remove(os.path.join(example_path, file))
                    else:
                        finished[domain].append(example_id)

    if not finished:
        return total_file_json

    for domain, examples in finished.items():
        if domain in total_file_json:
            total_file_json[domain] = [
                x for x in total_file_json[domain] if x not in examples
            ]

    return total_file_json


def get_result(action_space, use_model, observation_type, result_dir, total_file_json):
    target_dir = os.path.join(result_dir, action_space, observation_type, use_model)
    if not os.path.exists(target_dir):
        print("New experiment, no result yet.")
        return None

    all_result = []

    for domain in os.listdir(target_dir):
        domain_path = os.path.join(target_dir, domain)
        if os.path.isdir(domain_path):
            for example_id in os.listdir(domain_path):
                example_path = os.path.join(domain_path, example_id)
                if os.path.isdir(example_path):
                    if "result.txt" in os.listdir(example_path):
                        try:
                            all_result.append(
                                float(
                                    open(
                                        os.path.join(example_path, "result.txt"), "r"
                                    ).read()
                                )
                            )
                        except:
                            all_result.append(0.0)

    if not all_result:
        print("New experiment, no result yet.")
        return None
    else:
        print("Current Success Rate:", sum(all_result) / len(all_result) * 100, "%")
        return all_result


if __name__ == "__main__":
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    args = config()

    with open(args.test_all_meta_path, "r", encoding="utf-8") as f:
        test_all_meta = json.load(f)

    if args.domain != "all":
        test_all_meta = {args.domain: test_all_meta[args.domain]}

    test_file_list = get_unfinished(
        args.action_space,
        args.model,
        args.observation_type,
        args.result_dir,
        test_all_meta,
    )
    left_info = ""
    for domain in test_file_list:
        left_info += f"{domain}: {len(test_file_list[domain])}\n"
    logger.info(f"Left tasks:\n{left_info}")

    get_result(
        args.action_space,
        args.model,
        args.observation_type,
        args.result_dir,
        test_all_meta,
    )
    test(args, test_file_list)
