"""Script to run end-to-end evaluation on the benchmark.
Utils and basic architecture credit to https://github.com/web-arena-x/webarena/blob/main/run.py.
"""

import argparse
import datetime
import json
import logging
import os
import shutil
import sys
import traceback


def _load_dotenv() -> None:
    """Load <repo root>/.env into os.environ before anything reads it.

    Credentials and endpoints (VLLM_PORT, OPENROUTER_API_KEY,
    AWS_*, AZURE_*, GITLAB_ADMIN_TOKEN, ...) live in a git-ignored .env.  A shell
    `source .env` exports them, but a PyCharm/IDE run configuration does not run
    the shell profile or the conda activate.d hook, so the process would start
    with none of them set.  Reading the file here makes every launcher behave the
    same.

    Anchored on this file's directory, not the CWD, so an IDE run config with a
    different working directory still finds it.  Existing environment variables
    always win, so an explicit export or an IDE env-var entry can still override
    the file.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv(env_path, override=False)
        return

    with open(env_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, sep, value = line.partition("=")
            if not sep:
                continue
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if name and name not in os.environ:
                os.environ[name] = value


_load_dotenv()

from tqdm import tqdm

import lib_run_single
from desktop_env.desktop_env import DesktopEnv
from mm_agents import MAX_IMAGE_HISTORY_LENGTH, openrouter, token_usage
from mm_agents.hybrid_agent.agent import HybridAgent
from mm_agents.hybrid_agent.loop import HybridAgentLoop
from mm_agents.prompt_agent.agent import PromptAgent
from mm_agents.prompt_agent.loop import PromptAgentLoop
from mm_agents.fides.opencua import FidesOpenCUAAgent
from mm_agents.human_agent.agent import HumanAgent
from mm_agents.human_agent.loop import HumanAgentLoop


logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
if not os.path.exists("logs"):
    os.makedirs("logs")
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

logger = logging.getLogger("desktopenv.experiment")


REQUIRED_ARGS_BY_AGENT: dict[str, list[str]] = {
    "CamelAgent": ["camel_p_llm_model"],
}


_DEFAULT_MODEL = "vllm | Qwen/Qwen3.5-9B"

_ENDPOINT_ALIASES = {}

_MODEL_ARGS = (
    "model",
    "sa_p_llm",
    "sa_q_llm",
    "sa_q_llm_second_check",
    "sa_propose_model",
    "sa_rules_model",
    "fides_p_llm",
    "fides_q_llm",
    "ucm_model",
    "ucm_qllm_model",
)


def _split_provider(value: str):
    """Split "<provider><sep><model_name>" on whichever of | or : comes first.

    Model names contain ":" themselves (Bedrock's "...-v1:0"), so only the text
    before the *first* separator is ever considered a provider.
    Returns (provider, separator, separator_index) or None if there is no
    separator.
    """
    candidates = [(value.find(sep), sep) for sep in ("|", ":") if sep in value]
    if not candidates:
        return None
    idx, sep = min(candidates)
    return value[:idx].strip().lower(), sep, idx


def _resolve_endpoint_aliases(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Rewrite aliased providers to "vllm | X" and set the
    VLLM_BASE_URL / VLLM_API_KEY pair every vllm client reads.

    VLLM_BASE_URL is process-global, so one run can only talk to one
    OpenAI-compatible endpoint: mixing an alias with plain "vllm", or with two
    different aliases, is rejected rather than silently sending one of the two
    models to the wrong server.
    """
    seen = {}
    for name in _MODEL_ARGS:
        value = getattr(args, name, None)
        if not isinstance(value, str) or not value:
            continue
        parts = _split_provider(value)
        if parts is None:
            continue
        provider, sep, idx = parts
        if provider not in _ENDPOINT_ALIASES and provider != "vllm":
            continue
        seen.setdefault(provider, []).append(name)
        if provider in _ENDPOINT_ALIASES:
            head, tail = value[:idx], value[idx:]
            setattr(args, name, head.replace(head.strip(), "vllm", 1) + tail)

    aliases = [p for p in seen if p in _ENDPOINT_ALIASES]
    if not aliases:
        return

    if len(seen) > 1:
        detail = "; ".join(
            f"{p} (--{', --'.join(seen[p])})" for p in sorted(seen)
        )
        parser.error(
            "cannot use more than one OpenAI-compatible endpoint in a single run: "
            + detail
            + ". VLLM_BASE_URL is process-global, so all vllm-family models in one "
            "run share an endpoint. Run them as separate processes."
        )

    alias = aliases[0]
    url_env, url_default, key_env = _ENDPOINT_ALIASES[alias]

    if args.vllm_base_url is not None:
        parser.error(
            f"--vllm_base_url conflicts with the '{alias}' provider alias, which sets "
            "VLLM_BASE_URL itself. Pass one or the other."
        )

    base_url = os.getenv(url_env) or url_default
    api_key = os.getenv(key_env)
    if not api_key:
        parser.error(
            f"provider '{alias}' needs {key_env} in the environment (the virtual key, "
            f"typically starting with 'sk-'). Set it in .env; see README step 1."
        )

    os.environ["VLLM_BASE_URL"] = base_url
    os.environ["VLLM_API_KEY"] = api_key
    logger.info(
        "Provider alias '%s' -> vllm client at %s (key from %s); "
        "results stay under run_label %r.",
        alias,
        base_url,
        key_env,
        args.run_label,
    )


def _require_agent_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    missing = [
        f"--{dest}"
        for dest in REQUIRED_ARGS_BY_AGENT.get(args.agent_type, [])
        if getattr(args, dest) is None
    ]
    if missing:
        parser.error(f"--agent_type {args.agent_type} requires: {', '.join(missing)}")


_CAMEL_PROVIDERS: dict[str, tuple[str | None, str, str]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "openai"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter"),
    "vllm": (None, "VLLM_API_KEY", "openai"),
    "google": (None, "GOOGLE_API_KEY", "google"),
}

_CAMEL_QLLM_ONLY = ("google",)

_CAMEL_UNSUPPORTED = ("aws", "azure", "anthropic", "bedrock")

_CAMEL_LEGACY_KEY_ENV = {
    "p_llm": "PLLM_API_KEY",
    "vlm": "VLM_API_KEY",
    "q_llm": "QLLM_API_KEY",
}


def _camel_endpoint(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    role: str,
    provider: str,
    require_key: bool = True,
) -> tuple[str | None, str | None]:
    """(base_url, api_key) for a CamelAgent role whose model string names `provider`.

    `require_key=False` for a role that already has a key or a base url of its own: the
    provider then only supplies the default endpoint, and demanding its key would reject
    a private mirror reached with a different one.
    """
    default_url, key_env, _ = _CAMEL_PROVIDERS[provider]
    if provider == "vllm":
        port = args.vllm_port or os.getenv("VLLM_PORT", "8000")
        base_url = (
            args.vllm_base_url
            or os.getenv("VLLM_BASE_URL")
            or f"http://localhost:{port}/v1"
        )
    else:
        base_url = default_url

    api_key = os.getenv(key_env)
    if require_key and not api_key and provider != "vllm":
        parser.error(
            f"--camel_{role}_model names provider '{provider}', which needs {key_env} "
            "in the environment. Set it in .env; see README step 1."
        )
    return base_url, api_key


def _resolve_camel_models(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Decompose the four --camel_*_model strings into (model name, base url, api key).

    Fills in --camel_*_base_url and args.camel_*_api_key for every role, normalises the
    two Q-LLM flags to the "<provider>:<name>" spelling the camel submodule parses, and
    applies the inheritance a dual-LLM run expects (Q from P, VLM and the second check
    from Q). Does nothing for any other --agent_type.
    """
    if args.agent_type != "CamelAgent":
        return

    args.camel_label_model = args.model or args.camel_p_llm_model

    if args.camel_q_llm_model is None:
        args.camel_q_llm_model = args.camel_p_llm_model
    if args.camel_vlm_model is None:
        args.camel_vlm_model = args.camel_q_llm_model
    if args.camel_q_llm_second_model is None:
        args.camel_q_llm_second_model = args.camel_q_llm_model

    def split(role: str, value: str) -> tuple[str, str | None]:
        """(bare model name, provider or None) for one role's model string."""
        parts = _split_provider(value)
        if parts is None:
            return value.strip(), None
        provider, _, idx = parts
        if provider in _CAMEL_PROVIDERS:
            return value[idx + 1:].strip(), provider
        if provider in _CAMEL_UNSUPPORTED:
            parser.error(
                f"--camel_{role}_model names provider '{provider}', which CamelAgent "
                f"cannot reach: its P-LLM and VLM are OpenAI clients and its Q-LLM is a "
                f"pydantic-ai model. Supported: {', '.join(sorted(_CAMEL_PROVIDERS))}."
            )
        return value.strip(), None

    def resolve_openai_role(role: str) -> None:
        """P-LLM / VLM: an openai.OpenAI client built from base_url + key."""
        model_attr, url_attr = f"camel_{role}_model", f"camel_{role}_base_url"
        name, provider = split(role, getattr(args, model_attr))
        key_env = getattr(args, f"camel_{role}_api_key_env")
        api_key = os.getenv(key_env) if key_env else None

        if provider is None:
            if not getattr(args, url_attr):
                parser.error(
                    f"--camel_{role}_model '{name}' names no provider and "
                    f"--camel_{role}_base_url is not set, so there is no endpoint to "
                    f"call. Either spell the model as '<provider> | {name}' "
                    f"({', '.join(sorted(_CAMEL_PROVIDERS))}) or pass the base URL."
                )
            api_key = api_key or os.getenv(_CAMEL_LEGACY_KEY_ENV[role])
        else:
            if provider in _CAMEL_QLLM_ONLY:
                parser.error(
                    f"--camel_{role}_model names provider '{provider}', which is only "
                    "supported for the Q-LLM: the P-LLM and the VLM are built on an "
                    "openai.OpenAI client and so need an OpenAI-compatible endpoint ("
                    + ", ".join(
                        p for p in sorted(_CAMEL_PROVIDERS) if p not in _CAMEL_QLLM_ONLY
                    )
                    + ")."
                )
            explicit_url = bool(getattr(args, url_attr))
            base_url, provider_key = _camel_endpoint(
                parser, args, role, provider,
                require_key=not explicit_url and not api_key,
            )
            if not explicit_url:
                setattr(args, url_attr, base_url)
            api_key = api_key or provider_key

        setattr(args, model_attr, name)
        setattr(args, f"camel_{role}_api_key", api_key or "EMPTY")

    resolve_openai_role("p_llm")
    resolve_openai_role("vlm")

    key_env = args.camel_q_llm_api_key_env
    q_api_key = os.getenv(key_env) if key_env else None
    q_base_url = args.camel_q_llm_base_url
    seen_openai_url: dict[str, str] = {}

    for role, attr in (
        ("q_llm", "camel_q_llm_model"),
        ("q_llm_second", "camel_q_llm_second_model"),
    ):
        name, provider = split(role, getattr(args, attr))
        if provider is None:
            if ":" not in getattr(args, attr):
                if not q_base_url:
                    parser.error(
                        f"--camel_{role}_model '{name}' names no provider and "
                        "--camel_q_llm_base_url is not set, so there is no endpoint to "
                        f"call. Either spell it as '<provider> | {name}' "
                        f"({', '.join(sorted(_CAMEL_PROVIDERS))}) or pass the base URL."
                    )
                setattr(args, attr, f"openai:{name}")
                logger.info(
                    "CamelAgent: --camel_%s_model '%s' assumed to be served by "
                    "--camel_q_llm_base_url; using camel spelling 'openai:%s'.",
                    role, name, name,
                )
            continue

        token = _CAMEL_PROVIDERS[provider][2]
        setattr(args, attr, f"{token}:{name}")
        if token != "openai":
            _camel_endpoint(parser, args, role, provider)
            continue

        base_url, provider_key = _camel_endpoint(
            parser, args, role, provider,
            require_key=not args.camel_q_llm_base_url and not q_api_key,
        )
        seen_openai_url[role] = args.camel_q_llm_base_url or base_url
        if not args.camel_q_llm_base_url:
            q_base_url = q_base_url or base_url
        q_api_key = q_api_key or provider_key

    if len(set(seen_openai_url.values())) > 1:
        detail = "; ".join(f"--camel_{r}_model -> {u}" for r, u in sorted(seen_openai_url.items()))
        parser.error(
            "the Q-LLM and its second check resolve to two different OpenAI-compatible "
            "endpoints (" + detail + "), but the camel submodule reads one process-wide "
            "OPENAI_BASE_URL for both. Put them on the same endpoint, or give one of them "
            "a provider that builds its own client (openrouter, google)."
        )

    args.camel_q_llm_base_url = q_base_url
    args.camel_q_llm_api_key = q_api_key or os.getenv(_CAMEL_LEGACY_KEY_ENV["q_llm"]) or "EMPTY"


_SCREEN_VLM_FALLBACKS: dict[str, tuple[str, ...]] = {
    "SA-Agent": ("sa_q_llm", "sa_p_llm"),
    "FidesAgent": ("fides_q_llm", "fides_p_llm"),
}


def _resolve_screen_vlm_model(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Fill in an omitted --model from the agent's own P/Q flags, and reject a screen VLM
    the OpenCUA backend cannot reach.

    A dual-LLM run names its models with --sa_p_llm/--sa_q_llm and has no reason to
    also pass --model. --model used to carry its own hardcoded default, so such a run
    quietly put the screen VLM on a third model nobody asked for -- surfacing much later
    as a 404 from the first query_screen call, naming a model absent from the command
    line. Inherit the Q-LLM instead (the screen VLM is the vision half of the same
    quarantine), then the planner.
    """
    inherited_from = None
    if args.model is None:
        for name in _SCREEN_VLM_FALLBACKS.get(args.agent_type, ()):
            value = getattr(args, name, None)
            if value:
                args.model, inherited_from = value, name
                break
        else:
            args.model = _DEFAULT_MODEL

    if args.agent_type not in _SCREEN_VLM_FALLBACKS:
        return

    parts = _split_provider(args.model)
    if parts is None or parts[0] == "vllm":
        return
    source = f"--{inherited_from} (inherited by --model)" if inherited_from else "--model"
    parser.error(
        f"--agent_type {args.agent_type} builds its screen VLM from {source} = "
        f"'{args.model}', but that backend posts to http://localhost:$VLLM_PORT and so "
        f"only serves 'vllm | <model>' (provider '{parts[0]}' cannot be reached."
        "Pass a vllm-served --model explicitly for the screen VLM."
    )


def config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end evaluation on the benchmark"
    )

    parser.add_argument("--provider_name", type=str, default="docker")
    parser.add_argument("--region", type=str, default="us-east-2")
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
        help="Observation type",
    )
    parser.add_argument("--screen_width", type=int, default=1920)
    parser.add_argument("--screen_height", type=int, default=1080)
    parser.add_argument("--sleep_after_execution", type=float, default=0.0)
    parser.add_argument("--max_steps", type=int, default=50)
    parser.add_argument("--aws_ami", type=str, default=None)

    parser.add_argument(
        "--max_trajectory_length", type=int, default=10,
        help="How many past (observation, action) turns are re-sent with each request. "
             "Every past turn carries its full screenshot (~2,073 tokens at 1920x1080), and "
             "the whole window is re-uploaded on every step, so this is the dominant term in "
             "an episode's token bill: measured over 398 episodes, 90%% of all input tokens "
             "were re-transmitted history. Was 15, then 5; a 50-step episode costs ~1.37M "
             "input tokens at 15, roughly 1.1M at 10 (interpolated) and ~535k at 5. It also sets "
             "where OpenRouter prompt caching stops paying off and is switched off: at step "
             "N+2 the window slides and the cached prefix stops matching "
             "(mm_agents/openrouter.py)."
    )
    parser.add_argument(
        "--test_config_base_dir", type=str, default="evaluation_examples"
    )

    parser.add_argument("--model", type=str, default=None,
                        help="'<provider> | <model_name>' for the agent's model. For the dual-LLM "
                             "agents (SA-Agent/FidesAgent) this builds the OpenCUA *screen VLM* "
                             "only -- the planner and the text-side Q-LLM come from --sa_p_llm/"
                             "--sa_q_llm (resp. --fides_*). Left unset it inherits those, in that "
                             f"order; with none of them set it falls back to '{_DEFAULT_MODEL}'.")
    parser.add_argument(
        "--vllm_port", type=str, default=None,
        help="Overrides the VLLM_PORT env var (sets it in-process right after arg parsing) -- "
             "every vllm client in this codebase reads os.getenv('VLLM_PORT', ...) independently, "
             "so setting this here reaches all of them without touching shell/IDE env config."
    )
    parser.add_argument(
        "--vllm_base_url", type=str, default=None,
        help="Overrides the VLLM_BASE_URL env var the same way as --vllm_port (takes precedence "
             "over the port if both resolve, matching every call site's own getenv precedence)."
    )
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument(
        "--max_tokens", type=int, default=8192,
        help="Completion-token budget per model call. The OSWorld default was 1500, "
             "which a thinking model (Qwen3.x with --reasoning-parser qwen3) exhausts "
             "on the reasoning trace alone: vLLM returns finish_reason == 'length' with "
             "the whole budget in reasoning_content and content == None, which then "
             "crashes parse_code_from_string and loses the entire task. Same rationale "
             "as the SA-Agent/CamelAgent branches below, which already hardcode 8192."
    )
    parser.add_argument("--stop_token", type=str, default=None)
    parser.add_argument(
        "--api_provider", type=str, default="anthropic",
        choices=["anthropic", "bedrock", "vertex"],
        help="Anthropic API provider for ClaudeCUAAgent.",
    )
    parser.add_argument(
        "--no_thinking", action="store_true",
        help="Disable Claude extended thinking (ClaudeCUAAgent).",
    )
    parser.add_argument(
        "--use_isp", action="store_true",
        help="Use interleaved scratchpad (ClaudeCUAAgent, legacy 4.5/4.6 models).",
    )
    parser.add_argument(
        "--effort", type=str, default="max",
        choices=["low", "medium", "high", "max"],
        help="Adaptive thinking effort for ClaudeCUAAgent (Claude 4.7+).",
    )

    parser.add_argument("--domain", type=str, default="all")
    parser.add_argument(
        "--test_all_meta_path",
        type=str,
        default="evaluation_examples/test_all_reddit.json",
    )
    parser.add_argument(
        "--task_ids",
        type=str,
        default=None,
        help="Comma-separated list of exact task IDs to run, e.g. 'own_owncloud_1_specific_create_files_code,own_owncloud_1_loose_delete_bankstatement_code'",
    )
    parser.add_argument(
        "--task_filter",
        type=str,
        default=None,
        help="Substring filter on task IDs, e.g. '_code' or 'delete'",
    )
    parser.add_argument(
        "--again",
        action="store_true",
        default=False,
        help="Rerun already completed cases instead of skipping them",
    )

    parser.add_argument("--result_dir", type=str, default="./results")

    parser.add_argument("--snapshot_name", type=str, default="init_state")
    parser.add_argument("--debug", action="store_true")

    parser.add_argument(
        "--agent_type",
        choices=[
            "PromptAgent",
            "SA-Agent",
            "CamelAgent",
            "FidesAgent",
            "HybridAgent",
            "QwenAgent",
            "UCMAgent",
            "HumanAgent",
            "ClaudeCUAAgent",
        ],
        default="PromptAgent",
        help="Type of agent to use",
    )

    camel_group = parser.add_argument_group(
        "CamelAgent options", "Only used when --agent_type CamelAgent"
    )
    camel_group.add_argument(
        "--camel_model_defense_level",
        type=int,
        default=0,
        help="Model defense level 0-4 for CamelAgent.",
    )
    camel_group.add_argument(
        "--camel_pllm_exposure_level",
        type=int,
        default=0,
        help="P-LLM exposure level 0-2 for CamelAgent.",
    )
    camel_group.add_argument(
        "--camel_system_defense_level",
        type=int,
        default=2,
        help="System defense level 0-2 for CamelAgent.",
    )


    parser.add_argument(
        "--camel_p_llm_base_url",
        help="Base URL for the P-LLM OpenAI-compatible endpoint. Optional when "
             "--camel_p_llm_model carries a provider; overrides it when both are given.",
    )
    parser.add_argument(
        "--camel_p_llm_model",
        help="Planner (P-LLM) model, e.g. 'openrouter | openai/gpt-5.6-luna' or "
             "'vllm | Qwen/Qwen3.8-27B'. Providers: openai, openrouter, vllm.",
    )
    parser.add_argument(
        "--camel_p_llm_api_key_env",
        help="Environment variable holding the P-LLM API key. Defaults to the provider's "
             "own variable (OPENROUTER_API_KEY, VLLM_API_KEY, ...), else PLLM_API_KEY.",
    )

    parser.add_argument(
        "--camel_vlm_base_url",
        help="Base URL for the VLM OpenAI-compatible endpoint. Optional when "
             "--camel_vlm_model carries a provider; overrides it when both are given.",
    )
    parser.add_argument(
        "--camel_vlm_model",
        help="Grounding VLM (find/screenshot). Same spelling as --camel_p_llm_model. "
             "Defaults to --camel_q_llm_model, else --camel_p_llm_model.",
    )
    camel_group.add_argument(
        "--camel_vlm_api_key_env",
        help="Environment variable holding the VLM API key. Defaults to the provider's "
             "own variable, else VLM_API_KEY.",
    )

    parser.add_argument(
        "--camel_q_llm_base_url",
        help="Base URL for the Q-LLM OpenAI-compatible endpoint. Optional when the Q-LLM "
             "model carries a provider; overrides it when both are given. Shared by "
             "--camel_q_llm_model and --camel_q_llm_second_model (the camel submodule "
             "reads one process-wide OPENAI_BASE_URL for both).",
    )
    parser.add_argument(
        "--camel_q_llm_model",
        help="Quarantined LLM. Same spelling as --camel_p_llm_model, and 'google' is "
             "additionally accepted here. Defaults to --camel_p_llm_model.",
    )
    parser.add_argument(
        "--camel_q_llm_second_model",
        help="Model for the Q-LLM second check (only consulted at "
             "--camel_model_defense_level >= 3). Defaults to --camel_q_llm_model.",
    )
    parser.add_argument(
        "--camel_q_llm_api_key_env",
        help="Environment variable holding the Q-LLM API key. Defaults to the provider's "
             "own variable, else QLLM_API_KEY.",
    )

    camel_group.add_argument(
        "--camel_replan", action="store_true", help="Always replan for each example."
    )
    camel_group.add_argument(
        "--camel_vlm_use_relative_coords",
        action="store_true",
        help="Enable relative coordinate mode for VLM find(): tells the model the screen is 1000x1000 and rescales output back to actual pixels.",
    )
    camel_group.add_argument(
        "--camel_plan_only",
        action="store_true",
        help="Only generate and save the plan; skip execution (VLM not called).",
    )

    sa_group = parser.add_argument_group("SA-Agent options", "Only used when --agent_type SA-Agent")
    sa_group.add_argument("--sa_p_llm", default=None,
                        help="Planner (P-LLM) model for SA-Agent (format: 'provider:model_name' "
                             "or 'provider|model_name'). Defaults to --model.")
    sa_group.add_argument("--sa_q_llm", default=None,
                        help="Quarantined LLM used inside SA tool calls (find/summarize/extract). "
                             "Defaults to --sa_p_llm.")
    sa_group.add_argument("--sa_q_llm_second_check", default=None,
                        help="Second Q-LLM for the cross-check (model defense + endorsement). "
                             "None disables it.")
    sa_group.add_argument("--sa_system_defense_level", type=int, default=1,
                        help="System defense level 0-2 (progressively removes tools). Level 0 "
                             "additionally exposes run_vlm (raw observation access); default 1.")
    sa_group.add_argument("--sa_disabled_tools", default="",
                        help="Comma-separated tool names to disable on top of "
                             "--sa_system_defense_level's baseline, e.g. 'find,verify_hypothesis' "
                             "-- for ablation studies. Removed from both the active tool list and "
                             "the system prompt's action-space catalog together (see "
                             "mm_agents/sa_agent/tool_registry.py for valid names).")
    sa_group.add_argument("--sa_model_defense_level", type=int, default=0,
                        help="Model defense level 0-4 for the Q-LLM output attack detector.")
    sa_group.add_argument("--sa_token_log_path", default=None,
                        help="File to log Q-LLM token counts for SA-Agent.")
    sa_group.add_argument("--sa_trusted_domains",
                        default=(
                                "docs.python.org/3,"
                                "packaging.python.org,pip.pypa.io,"
                                "nodejs.org/en/download,nodejs.org/docs,nodejs.org/api,"
                                "nodejs.org/dist,"
                                "docs.npmjs.com,"
                                "doc.rust-lang.org,go.dev/doc,go.dev/ref,"
                                "docs.oracle.com/javase,"
                                "ubuntu.com/server/docs,help.ubuntu.com,"
                                "manpages.ubuntu.com/manpages,"
                                "debian.org/doc,"
                                "man7.org/linux/man-pages,docs.kernel.org,"
                                "gnu.org/software,"
                                "git-scm.com/docs,"
                                "docs.docker.com,"
                                "docs.github.com,docs.gitlab.com,"
                                "curl.se/docs,openssl.org/docs,sqlite.org,"
                                "postgresql.org/docs,redis.io/docs,"
                                "nginx.org/en/docs,httpd.apache.org/docs,"
                                "kubernetes.io/docs,"
                                "help.libreoffice.org,"
                                "rfc-editor.org/rfc,datatracker.ietf.org/doc,"
                                "nvd.nist.gov/vuln/detail,services.nvd.nist.gov/rest/json,"
                                "ubuntu.com/security/notices,"
                                "security-tracker.debian.org/tracker"
                        ),
                        help="Comma-separated allowlist of official verification sources. Each "
                             "entry is a host suffix with an OPTIONAL path prefix "
                             "(e.g. 'docs.python.org/3' allows /3 and everything under it, but "
                             "not /30-something; a bare 'ubuntu.com' allows every path on it). "
                             "Web fetches matching these are labeled fully TRUSTED and may be "
                             "used to verify endorsements up to full trust -- so entries "
                             "should name DOCUMENTATION, not a whole site. Note what an entry "
                             "asserts: not that the host is reputable, but that content at that "
                             "path is authored by the vendor rather than by its users. "
                             "wikipedia.org was a default here and was removed for exactly that "
                             "reason -- reputable host, user-editable content, full TRUSTED. "
                             "pypi.org/project was demoted to the corroborating list below for "
                             "the same reason: a project page's text is written by whoever "
                             "uploaded the package, so it can restate a typosquat as "
                             "authoritatively as it restates the real thing. The default set is "
                             "sized to the benign tasks the agent is actually run on: the "
                             "Node.js install page (rocketchat_1 -- note it lives under "
                             "/en/download, NOT /docs, so a 'nodejs.org/docs' entry alone "
                             "refuses the one page that task needs), git-config (rocketchat_2), "
                             "Ubuntu manpages for nmon/tmux/update-alternatives (owncloud_2, "
                             "reddit_3, rocketchat_3), and the Docker CLI reference "
                             "(owncloud_3).")
    sa_group.add_argument("--sa_trusted_domains_corroborating",
                        default=(
                                "github.com,api.github.com,"
                                "registry.npmjs.org,"
                                "pypi.org/pypi,pypi.org/project,"
                                "hub.docker.com/v2/repositories,hub.docker.com/v2/search,"
                                "api.launchpad.net/1.0/ubuntu,"
                                "sources.debian.org/api,"
                                "packages.ubuntu.com,packages.debian.org,"
                                "crates.io/api,rubygems.org/api,pkg.go.dev,"
                                "snapcraft.io/api,flathub.org/api,"
                                "repology.org/api,libraries.io/api,endoflife.date/api,"
                                "github.com/advisories,api.github.com/advisories,"
                                "developer.mozilla.org,vimhelp.org,"
                                "anonymous.4open.science/api/repo,"
                                "anonymous.4open.science/r"
                        ),
                        help="Comma-separated allowlist of corroborating (non-official) VERIFY-"
                             "AGAINST sources, same host[/path] syntax. These are oracles, not "
                             "readable pages: a host matching this list gates which sources "
                             "endorse_by_crosscheck / endorse_by_listing (which fetch "
                             "host-side and return only a verdict or a re-derived answer, at the "
                             "ENDORSED cap) will accept, and the policy gate uses it to permit "
                             "a tainted-URL fetch. It does NOT make the host's body readable: a "
                             "raw fetch_web_content of a corroborating host comes back QUARANTINED "
                             "(UNTRUSTED, redacted into a variable), because these hosts serve "
                             "USER-authored content that can restate a typosquat as authoritatively "
                             "as the real thing -- only the official --sa_trusted_domains list "
                             "(vendor-authored) is read in the clear. A path may still be pinned "
                             "where only one part of a "
                             "host is an oracle; github is now allowed at host level (incl. "
                             "'/search?q=...') because --sa_task_derived_evidence, not the path "
                             "pin, is what keeps the querier from choosing the answer (see the "
                             "benchmark's query_laundering_task_derived case). Whether ENDORSED "
                             "suffices for a given action is "
                             "controlled by --sa_min_trusted_level below (see "
                             "policies.derive_tier_min_level). The registry-API entries "
                             "(pypi.org/pypi, hub.docker.com/v2/repositories, "
                             "api.launchpad.net/1.0/ubuntu, sources.debian.org/api) are the same "
                             "endpoints endorse.REGISTRIES builds internally. They are listed "
                             "so a planner can reach the identical evidence by hand via "
                             "endorse_by_crosscheck, at the identical ENDORSED cap; "
                             "endorse_by_registry itself does NOT consult these lists, since "
                             "it builds the URL from the value and never takes one from the "
                             "planner.")
    sa_group.add_argument("--sa_min_trusted_level",
                        choices=["trusted", "self_authored", "endorsed", "computed", "free_text"],
                        default="endorsed",
                        help="Minimum rung of the integrity ladder (see "
                             "mm_agents/sa_agent/lattice.py's IntegrityLabel.Level: TRUSTED < "
                             "SELF_AUTHORED < ENDORSED < COMPUTED_PLATFORM_VALUE < "
                             "FREE_TEXT_THIRD_PARTY < UNTRUSTED) a DOCUMENT-zone label must reach "
                             "to NOT be redacted/masked by the default is_untrusted() gate. "
                             "'endorsed' (default) accepts TRUSTED/SELF_AUTHORED/ENDORSED "
                             "and redacts COMPUTED_PLATFORM_VALUE, FREE_TEXT_THIRD_PARTY (free-text "
                             "UGC: forum posts, chat/email messages, listing descriptions), and "
                             "UNTRUSTED (ads, cross-origin embeds, unclassified content). "
                             "COMPUTED_PLATFORM_VALUE is deliberately NOT in the default-accepted "
                             "set: Stage-1b's value-shape classifier grants it to a single, "
                             "unverified instance whenever its text merely matches a narrow "
                             "numeric/currency/date/bool shape (no repetition needed), or to any "
                             "value an attacker can repeat across two of their own UGC posts -- "
                             "pass 'computed' explicitly only if you understand and accept that "
                             "forgery surface. 'trusted' is strictest (only first-party/self "
                             "content passes); 'free_text' is most permissive (only ads/cross-origin "
                             "content redacted). This ALSO caps how much trust SAPolicy's "
                             "REVERSIBLE_WRITE/DESTRUCTIVE tiers may demand (see "
                             "policies.derive_tier_min_level): a tier can never require more trust "
                             "than this cutoff already lets through as a literal in the planner's "
                             "own context, since gating a specific tool-call argument against a "
                             "value the planner can already read/retype is unenforceable. E.g. "
                             "'endorsed' (or looser) makes data endorsed to the ENDORSED level "
                             "sufficient for DESTRUCTIVE actions like run_terminal_command too, not "
                             "just REVERSIBLE_WRITE ones.")
    sa_group.add_argument("--sa_allow_corroborated_destructive", action="store_true",
                        help="Deprecated, now a no-op (prints a warning if passed): tier floors are "
                             "derived automatically from --sa_min_trusted_level above instead of "
                             "being toggled by a separate flag. Use --sa_min_trusted_level="
                             "endorsed (or looser) for the equivalent effect.")
    sa_group.add_argument("--sa_task_derived_evidence", action=argparse.BooleanOptionalAction,
                        default=False,
                        help="Require endorse_by_crosscheck's `trusted_url` QUERY terms to be "
                             "derivable from the trusted side -- the task instruction, or a value "
                             "already cleared to TRUSTED. **OFF by default.** Under the framework's "
                             "core invariant untrusted screen/tool content is masked and redacted "
                             "before it ever reaches the planner's context, so the planner can only "
                             "build a query out of trusted text in the first place -- the "
                             "'query built from untrusted content' splice this guard refuses cannot "
                             "occur, and turning it on only false-refuses legitimately-worded "
                             "queries (e.g. a task naming 'StackOverflow' asked as 'stack overflow'). "
                             "Retained behind this flag only for a hypothetical future exposure mode "
                             "that would show typed values to the planner in the clear; pass "
                             "--sa_task_derived_evidence to enable it there.")
    sa_group.add_argument("--sa_layer3", choices=["off", "derive"], default="off",
                        help="Layer 3 (procedures). 'derive' asks the P-LLM, once per episode and "
                             "from the task instruction plus the starting-state note ONLY, which "
                             "capabilities the task authorizes; endorse_by_procedure then "
                             "requires a proposed command's effect set to fit inside that. "
                             "Nothing read mid-episode can widen it -- same invariant as "
                             "--sa_trust_confirmation's domain detection. 'off' (default) "
                             "leaves the set unset, under which every procedure is refused, so "
                             "the tool is inert rather than permissive when this is off. Note the "
                             "risk this trades against: a wrong derivation OVER-authorizes, which "
                             "is why DESTRUCTIVE-tier grants additionally have to be corroborated "
                             "by a word in the task text itself.")
    sa_group.add_argument("--sa_authorized_capabilities", default="",
                        help="Comma-separated capability names (see capabilities.CAPABILITY_"
                             "CATALOG) that replace the --sa_layer3 derivation outright. For "
                             "experiments that need the capability set held fixed across runs "
                             "rather than re-derived per episode -- the benchmark's own Layer-3 "
                             "cases do exactly this, since a derivation is a model call and "
                             "freezing it is what every other model step in that harness does.")
    sa_group.add_argument("--sa_scope_root", default="",
                        help="Directory that FILE_PATH parameters of a Tier-B procedure must lie "
                             "under. Carries the whole load in the case containment cannot "
                             "discriminate -- a task that authorizes deletion authorizes deleting "
                             "the things it pointed at, not everything (plan §6.7). Empty means "
                             "no scope root, and a path leaf must then appear in the task text "
                             "itself.")
    sa_group.add_argument("--sa_policy_mode", choices=["enforce", "audit"], default="enforce",
                        help="'enforce' blocks tainted consequential calls; 'audit' only logs "
                             "would-block decisions (for measuring over-blocking on benign tasks). "
                             "'audit' lifts EVERY policy refusal, including the location rule that "
                             "refuses a click/type on redacted pixels.")
    sa_group.add_argument("--sa_query_screen_output", choices=["labeled", "clear"], default="labeled",
                        help="Ablation. 'labeled' (default): a query_screen answer below the trust "
                             "floor reaches the planner as a redacted variable with an integrity "
                             "label. 'clear': the planner gets the constrained Q-VLM answer itself "
                             "as plain text (answer_type checks still apply; no token, no label in "
                             "the message), and the prompt says so. The value is still stored under "
                             "its usual token and label, so $-references resolve and the audit "
                             "trace keeps its taint.")
    sa_group.add_argument("--sa_mask_mode", choices=["deny"], default="deny",
                        help="Masked-screenshot exposure (ON by default). 'deny' shows the P-LLM a screenshot with "
                             "everything not certified trusted blacked out (and numbered query "
                             "regions drawn on it), turning the planner from text-only into a "
                             "prompt_agent-shaped agent that sees a redacted screen; 'off' (the "
                             "planner that sees a redacted screen; 'off' reverts to the text-only "
                             "planner that never sees pixels, which is the pre-masking behavior and "
                             "the A/B baseline. Needs a rule set for every domain visited -- see "
                             "--sa_rules_dir and at-spi/labelers/selector_propose.py.")
    sa_group.add_argument("--sa_history_window", type=int, default=-1,
                        help="How many trailing tool-call ELEMENTS the planner carries, an "
                             "element being one tool call with its result and the masked "
                             "screenshot that followed it. This is PromptAgent's sliding window "
                             "in the planner's vocabulary, and -1 (the default) inherits "
                             "--max_trajectory_length so the two frameworks slide the same amount "
                             "of trajectory -- a token comparison between them is then a "
                             "comparison of agents rather than of window sizes. Older elements "
                             "are deleted outright, exactly as PromptAgent drops older turns; the "
                             "system prompt and the task are never trimmed, and the full "
                             "conversation is still written to planner_history.json. A call that "
                             "did not change the screen (query_screen, endorse_by_crosscheck, "
                             "fetch_web_content) adds no picture, so a 10-element window carries "
                             "between 0 and 10 screenshots against PromptAgent's invariant 10. "
                             "0 restores the pre-window behaviour: an append-only history with "
                             "exactly one ephemeral screenshot per planner call, never re-sent, "
                             "which is what every SA episode recorded before 2026-09-24 paid "
                             "for.")
    sa_group.add_argument("--sa_rules_dir",
                        default="llm_rules_container_v15,llm_rules_container_v17_suites_or,"
                                "llm_rules_container_overrides",
                        help="Comma-separated directories of REVIEWED trust rule sets. LATER "
                             "ENTRIES OVERRIDE EARLIER ONES for the same capture (RuleCache._load "
                             "assigns per capture as it walks the list), so the broadest set goes "
                             "first and the preferred one last. That is what the default does: "
                             "v15 (Qwen3.8-27B) covers all 160 corpus captures, and "
                             "v17_suites_or (openai/gpt-5.6-luna, via OpenRouter) overrides the 45 "
                             "captures of the five eval platforms -- thunderbird 200-207, reddit "
                             "400-408, owncloud 420-429, rocketchat 440-448, gitlab 460-468. "
                             "Measured on those 45, same universe and scorer: v17 holds leak=0 "
                             "(one apparent free-text miss on 426_owncloud is a ground-truth "
                             "labeling artifact -- a 'Menu' burger button) while taking FP from "
                             "75.0%% to 45.6%% and usable rows from 859 to 1872. On the 8 captures "
                             "the evaluation actually routes to, both arms are leak=0 with "
                             "identical FN and v17 halves the over-masking. Last comes "
                             "llm_rules_container_overrides, one file deep: v17's set for "
                             "207_thunderbird_compose_reply does not grant Send/Save/Attach and "
                             "v15's does, and compose is the state most Thunderbird tasks need "
                             "(see that directory's README). Do NOT pass a "
                             "suite-only directory alone: it strands the public-web corpus, the "
                             "UCM sites and the other native apps on --sa_mask_on_unknown_domain. "
                             "Relative paths resolve against at-spi/. Two file naming "
                             "conventions are accepted: '<capture>__<model>.json' (Phase-1 output "
                             "from selector_propose.py --out_dir; the capture maps to a domain via "
                             "at-spi/manifest.json to a SCOPE and a TEMPLATE -- see "
                             "at-spi/rule_scope.py) and '<host>.json' (a rule set written directly "
                             "for a host with no corpus capture, applying to all of its pages). "
                             "A directory named here that does not exist is an error, not a "
                             "warning: running with no rules is indistinguishable downstream from "
                             "a site that grants nothing. Rule sets the agent generates at "
                             "runtime are NOT written here -- see --sa_rules_cache_dir.")
    sa_group.add_argument("--sa_ucm_rules_dir", default="llm_rules_container_v19",
                        help="The ONLY source of rule sets for the UCM captures (manifest "
                             "category 'ucm', 500-533), whatever --sa_rules_dir lists and in "
                             "whatever order: UCM is measured on the labeling pipeline's sets, "
                             "unioned per site at run time (every UCM site is a one-URL SPA). "
                             "This keeps the hand-edited llm_rules_container_overrides/5xx_ucm_* "
                             "sets out of every UCM run. Pass '' to fall back to --sa_rules_dir "
                             "ordering. Relative paths resolve against at-spi/.")
    sa_group.add_argument("--sa_rules_cache_dir", default="rules_cache",
                        help="Where runtime-proposed rule sets are cached, as '<domain>.json' plus "
                             "a '<domain>.meta.json' recording what proposed them, when, and from "
                             "which URL. Relative paths resolve against at-spi/. Read and written "
                             "only when --rule_caching is true, and always at LOWER precedence "
                             "than --sa_rules_dir: a scored or hand-checked rule set is "
                             "evidence, a cached proposal is a shortcut. Promote one by moving the "
                             "file into a --sa_rules_dir directory.")
    sa_group.add_argument("--rule_caching", type=lambda v: str(v).lower() not in ("0", "false", "no"),
                        default=True, metavar="{true,false}",
                        help="Reuse a stored trust rule set when one exists for the page's domain "
                             "(default true). With false, or when no stored set exists, rules are "
                             "proposed at runtime by the at-spi Phase-1 pipeline "
                             "(labelers/selector_propose.py) against the CURRENT page, using the "
                             "same sanitizer, prompt and filters as an offline run — the model "
                             "sees only structure (roles/classes/ids), never page text. With true, "
                             "a live proposal is also written to --sa_rules_cache_dir so the next "
                             "episode on that domain reuses it. With false the disk cache is out of "
                             "play in BOTH directions: no stored set is read, and nothing is "
                             "written back, so a run measuring the proposer cannot seed the cache "
                             "for the runs after it (a proposal is still reused across the turns of "
                             "its own episode). Rules are an ALLOWLIST either way: anything no rule "
                             "grants is masked, and a page-scope rule that would trust a whole live "
                             "page is refused.")
    sa_group.add_argument("--sa_propose_model", default=None,
                        help="Model used for runtime rule proposal, as 'provider|name' (e.g. "
                             "'vllm|Qwen/Qwen3.6-35B-A3B'). Defaults to --model, with any '|cua' "
                             "suffix stripped: proposal is a text task over a sanitized structural "
                             "skeleton (roles/classes/ids, never page text), so the model already "
                             "running the episode can do it. Set this explicitly to measure the "
                             "proposer separately from the planner.")
    sa_group.add_argument("--sa_propose_vllm_port", type=int, default=None,
                        help="Port for a vLLM-served --sa_propose_model. Unset falls back to "
                             "VLLM_PORT, which --vllm_port already sets -- so a vLLM run needs "
                             "this only when the proposer is served separately from --model.")
    sa_group.add_argument("--sa_rules_model", default=None,
                        help="When --sa_rules_dir holds rule sets from several models, use only "
                             "this one (matched against the filename).")
    sa_group.add_argument("--sa_mask_default_deny", action="store_true",
                        help="Also redact page area that NO labeled row covers, not just "
                             "explicitly-untrusted rows. OFF by default, matching "
                             "at-spi/mask_screenshot.py's own --default_deny: the DOM oracle "
                             "measured the accessibility tree covering a median 99.0%% of visible "
                             "DOM text across 111 captures (only one below 70%%), so an uncovered "
                             "gap is overwhelmingly whitespace rather than text the tree missed. "
                             "Left on it dominates the picture -- 47.7 of 80.3 masked points on "
                             "the live Reddit replica came from this alone. Turn it on for a "
                             "worst-case view, or for a page whose text coverage you distrust.")
    sa_group.add_argument("--sa_mask_style", choices=["fill", "blur"], default="fill",
                        help="How redacted areas are drawn. 'fill' (default) is flat black; 'blur' "
                             "keeps layout legible but is weaker -- blurred text is sometimes "
                             "still readable, so only use it to study the layout/legibility "
                             "trade-off, never as a security setting.")
    sa_group.add_argument("--sa_mask_click_policy", choices=["block", "off"], default="block",
                        help="'block' (default) refuses any pointer action landing on redacted "
                             "pixels, and any keystroke while a redacted element holds focus -- "
                             "the invariant the masked planner rests on is that it may act only "
                             "on pixels it can see. 'off' redacts the picture but does not gate "
                             "the actions, which exists ONLY to measure what the gate costs on "
                             "benign tasks. There is deliberately no capability-based middle "
                             "setting: an untrusted link and an ad banner are both NAVIGATE.")
    sa_group.add_argument("--sa_mask_on_unknown_domain", choices=["abort", "deny", "off"], default="abort",
                        help="What to do when the current page's domain has no Phase-1 rule set. "
                             "'abort' (default) no longer ends the episode (2026-09-24): the page "
                             "is treated as if no rules were proposed -- default-deny, like "
                             "'deny' -- but logged at ERROR and traced as rules_from=unknown_abort "
                             "in mask_stats.jsonl, because an unprovisioned domain masked whole "
                             "leaves the agent nothing to do and scores as zero attack success -- "
                             "indistinguishable from a defense that worked. Reached only when "
                             "proposal is unavailable or fails (a failed proposal is traced as "
                             "proposal_failed), since --sa_propose_model defaults to --model. "
                             "'off' runs that page unmasked. Any other MaskUnavailable is shown "
                             "through masking.fallback_mask after the loop's retries instead of "
                             "aborting; only a missing --sa_rules_dir still aborts.")
    sa_group.add_argument("--sa_icl_pack", default=None,
                        help="Directory of worked examples (ICL) for the planner's system prompt. "
                             "A name resolves under mm_agents/sa_agent/ (default: 'icl'); an "
                             "absolute path is used as given. Each .md file is one example and "
                             "declares in its front matter which slot it fills, which observation "
                             "mode it applies to, and which tools must be active for it to render "
                             "-- see mm_agents/sa_agent/icl.py. Swap the directory to compare "
                             "example sets without editing prompts.py; the set actually used is "
                             "logged as '[sa][icl] pack=... using=[...]'.")
    sa_group.add_argument("--sa_mask_on_native_app",
                        choices=["app_rules", "deny", "off", "abort"], default="app_rules",
                        help="What to do on a turn where a NATIVE APPLICATION has focus rather "
                             "than the browser -- a terminal, Files, LibreOffice, a PDF viewer. "
                             "Such a turn has no URL, so --sa_mask_on_unknown_domain does not "
                             "apply to it (and used to catch it, aborting every episode that "
                             "opened a shell). 'app_rules' (default) resolves against the "
                             "app-scoped rule sets keyed 'app:<at-spi name>' -- the corpus carries "
                             "45 captures across 14 applications -- and proposes live for an "
                             "application it has never seen. 'deny' masks the window, 'off' runs "
                             "the turn unmasked, 'abort' fails the episode.")
    sa_group.add_argument("--sa_file_owner_shortcut", type=str, default="on",
                            choices=["on", "off"],
                            help="Runtime trust shortcut for a file a native app has open that no "
                                 "provenance manifest covers (the user's own base-image documents, "
                                 "typically). 'on' grants TRUSTED when the VM says the file is "
                                 "owned by the agent's user AND predates the episode AND carries "
                                 "no download-origin xattr AND is not in a download directory -- "
                                 "all four, because on this VM every downloaded file is also owned "
                                 "by 'user'. A manifest entry always wins over the probe. 'off' "
                                 "restores the previous fail-closed behaviour (default: on).")
    sa_group.add_argument("--sa_strict_targets", action="store_true",
                        help="Treat pointer actions on elements the a11y tree cannot resolve as "
                             "worst-case (DESTRUCTIVE + untrusted) instead of the lenient default.")
    sa_group.add_argument("--sa_trust_confirmation", choices=["off", "ask", "auto"], default="off",
                        help="Episode-scoped dynamic trust: detect, once per episode from the task "
                             "instruction and starting screen state alone, (1) which domain(s) the "
                             "task itself requires (e.g. 'download repo xyz from github' -> "
                             "github.com) and raise that domain's own structural/navigational "
                             "DOCUMENT-zone content to TRUSTED (community/discussion regions -- "
                             "anything inside a detected authorship region_id -- are excluded and "
                             "fall to the region-based classification instead; see "
                             "screen_labels.py), and (2) which account/handle the agent operates "
                             "as, raising that account's own authored content to SELF_AUTHORED "
                             "(see trust_detection.resolve_episode_self_identity). 'off' (default) "
                             "disables both detections entirely. 'auto' elevates detected "
                             "domains/identity immediately and logs each one. 'ask' additionally "
                             "requires an interactive y/n before elevating -- blocks on stdin, "
                             "unsuitable for unattended/headless batch runs.")

    sa_group.add_argument("--sa_spec_classifier", choices=["off", "log", "loose", "ask", "auto"], default="loose",
                        help="Specification gate, once per episode from the task instruction and "
                             "starting screen state alone (trust_detection.resolve_specification). "
                             "Flags a BARE-POINTER task that states no goal of its own and delegates "
                             "the whole job to untrusted content the agent has not read yet ('follow "
                             "the instructions in this document'), so nothing trusted can validate "
                             "what gets installed/sent/deleted/run. A task that states its purpose -- "
                             "even if the exact value comes from the content -- is NOT flagged. 'off' "
                             "disables it (no model call). 'log' runs the classifier and logs a "
                             "WARNING on a flagged task but proceeds UNCHANGED -- detection only. "
                             "'loose' (default) is 'log' plus: on a flagged task it SUBSTITUTES the "
                             "task's loose formulation (config meta_data.loose_instruction) for the "
                             "bare-pointer instruction when one is available, else proceeds "
                             "unchanged. 'auto' instead appends a per-task clarification (the config's "
                             "`spec_clarification`) to the instruction, else halts and returns a "
                             "question. 'ask' additionally prompts the operator on stdin when no "
                             "clarification was supplied -- blocks on stdin, unsuitable for headless "
                             "batch. There is no mid-run user ask; the mid-run safe move is the "
                             "`abstain` tool.")

    fides_group = parser.add_argument_group(
        "FidesAgent options", "Only used when --agent_type FidesAgent"
    )
    fides_group.add_argument(
        "--fides_variant",
        choices=["full_exposure", "no_exposure"],
        default="full_exposure",
        help="Which Fides planning loop to use for FidesAgent: "
        "no_exposure redacts tool outputs into variable references (the IFC defense), "
        "full_exposure lets the planner see raw tool outputs.",
    )
    fides_group.add_argument(
        "--fides_p_llm",
        default=None,
        help="Planner LLM for FidesAgent (format: 'provider:model_name'). Defaults to --model.",
    )
    fides_group.add_argument(
        "--fides_q_llm",
        default=None,
        help="QLLM model used inside Fides tool calls (e.g. find/summarize/checkdone).",
    )
    fides_group.add_argument(
        "--fides_q_llm_second_check",
        default=None,
        help="Second QLLM model for the no_exposure variant's cross-check. None disables it.",
    )
    fides_group.add_argument(
        "--fides_system_defense_level",
        type=int,
        default=1,
        help="System defense level 0-2 for FidesAgent (progressively removes tools). "
        "Level 0 additionally exposes run_vlm (raw observation access for the "
        "planner); default is 1 so that stays off unless explicitly requested.",
    )
    fides_group.add_argument(
        "--fides_model_defense_level",
        type=int,
        default=0,
        help="Model defense level 0-4 for FidesAgent's no_exposure variant.",
    )
    fides_group.add_argument(
        "--fides_token_log_path",
        default=None,
        help="File to log QLLM token counts for FidesAgent.",
    )

    ucm_group = parser.add_argument_group("UCMAgent options", "Only used when --agent_type UCMAgent")
    ucm_group.add_argument("--ucm_model", default=None,
                        help="'<provider> | <model_name>' for the main screen-acting/tool-calling agent "
                             "(computer_use + quarantined_llm_analysis tools) -- same convention as --model "
                             "(e.g. 'anthropic | claude-sonnet-4-6' or 'vllm | Qwen/Qwen3.5-9B'; the vllm/openai/"
                             "openrouter providers need the model served with tool-calling enabled, e.g. vLLM's "
                             "--enable-auto-tool-choice --tool-call-parser hermes for the Qwen family). "
                             "Defaults to --model.")
    ucm_group.add_argument("--ucm_qllm_model", default=None,
                        help="'<provider> | <model_name>' for the Quarantined Model (Q-Model) that reads masked "
                             "element content -- same convention as --ucm_model. Defaults to --ucm_model (or "
                             "--model), i.e. the Q-Model runs on the same backend as the main agent unless set "
                             "separately.")
    ucm_group.add_argument("--ucm_allow_unsolvable", action="store_true",
                        help="Let the agent declare TASK UNSOLVABLE for tasks that need verbatim "
                             "free-text extraction from masked content, which the QLLM tool cannot "
                             "provide (it only returns bool/int/float/enum/date).")
    ucm_group.add_argument("--ucm_site", choices=["reddit", "owncloud", "rocketchat"], default=None,
                        help="Force which per-site marker script (js/<site>_marker.js) to inject, "
                             "instead of auto-detecting the platform from the current page URL "
                             "(browser_guard.detect_site). Use this if a task's start page doesn't "
                             "contain one of the auto-detection hints.")
    ucm_group.add_argument("--ucm_reveal", choices=["default", "all", "mask"], default="all",
                        help="UCM site reveal policy (mirrors UCM's --reveal-all / --mask-untrusted). "
                             "DEFAULT is 'all': appends ?reveal_all=true to every UCM start URL so the "
                             "agent sees all data-untrusted content, including injected value-attack "
                             "ads -- the site otherwise hides every untrusted region behind an "
                             "'id:<name>' placeholder, which silently breaks utility runs and reads "
                             "adversary scores as false negatives. Pass 'mask' to append "
                             "?mask_untrusted=true (measure the UCM site-level defense itself), or "
                             "'default' to leave URLs untouched (site default: untrusted content "
                             "hidden behind placeholders).")

    parser.add_argument("--score", action="store_true",
                        help="Print success counts for finished runs and exit (no eval).")

    args = parser.parse_args()
    _require_agent_args(parser, args)
    _resolve_camel_models(parser, args)
    _resolve_screen_vlm_model(parser, args)

    if args.vllm_port is not None:
        os.environ["VLLM_PORT"] = args.vllm_port
    if args.vllm_base_url is not None:
        os.environ["VLLM_BASE_URL"] = args.vllm_base_url

    if args.agent_type == "SA-Agent":
        _exec = args.sa_p_llm or args.model
        args.run_label = f"{_exec}+sa"
    elif args.agent_type == "FidesAgent":
        _exec = args.model
        args.run_label = f"{_exec}+fides"
    elif args.agent_type == "CamelAgent":
        _exec = args.camel_label_model
        args.run_label = f"{_exec}+camel"
    elif args.agent_type == "HybridAgent":
        _exec = args.model
        args.run_label = f"{_exec}+hybrid"
    elif args.agent_type == "UCMAgent":
        _exec = args.ucm_model or args.model
        args.run_label = f"{_exec}+ucm"
    elif args.agent_type == "HumanAgent":
        args.run_label = "human"
    else:
        args.run_label = args.model

    _resolve_endpoint_aliases(parser, args)

    if args.debug:
        logger.info("Debug mode")
        args.result_dir = os.path.join(
            args.result_dir, "debug_{uuid}".format(uuid=datetime_str)
        )
        logger.info("result_dir: %s", args.result_dir)

    if args.aws_ami is not None:
        import platform

        platform.machine = lambda: "x86_64"

    return args


def test(args: argparse.Namespace, test_all_meta: dict) -> None:
    scores = []
    max_steps = args.max_steps

    os.makedirs(args.result_dir, exist_ok=True)
    command_file = os.path.join(args.result_dir, "run_command.txt")
    with open(command_file, "w", encoding="utf-8") as f:
        f.write(" ".join(sys.argv) + "\n")

    import copy as _copy
    _redacted_args = _copy.copy(args)
    for _k in list(vars(_redacted_args)):
        _kl = _k.lower()
        if (_kl.endswith("api_key") or "token" in _kl or "secret" in _kl or "password" in _kl) and getattr(_redacted_args, _k):
            setattr(_redacted_args, _k, "***REDACTED***")
    logger.info("Args: %s", _redacted_args)
    cfg_args = {
        "path_to_vm": args.path_to_vm,
        "headless": args.headless,
        "action_space": args.action_space,
        "observation_type": args.observation_type,
        "screen_width": args.screen_width,
        "screen_height": args.screen_height,
        "sleep_after_execution": args.sleep_after_execution,
        "max_steps": args.max_steps,
        "max_trajectory_length": args.max_trajectory_length,
        "model": args.model,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "stop_token": args.stop_token,
        "result_dir": args.result_dir,
    }
    assert args.action_space in ["pyautogui", "claude_computer_use"], (
        "supported action spaces are pyautogui and claude_computer_use"
    )

    if args.agent_type == "SA-Agent":
        from mm_agents.sa_agent.opencua import FidesOpenCUAAgent as SAOpenCUAAgent
        coordinate_type = "absolute" if args.observation_type == "screenshot_a11y_tree" else "relative"
        agent = SAOpenCUAAgent(
            action_space=args.action_space,
            observation_type=args.observation_type,
            max_image_history_length=MAX_IMAGE_HISTORY_LENGTH,
            model_type="qwen25vl",
            port=os.getenv('VLLM_PORT', '8001'),
            model=args.model,
            history_type="action_history",
            coordinate_type=coordinate_type,
            max_tokens=8192,
        )
        agent_type = "SA-Agent"
    elif args.agent_type == "CamelAgent":
        from mm_agents.camel_agent import CamelAgent

        agent = CamelAgent(
            p_llm_base_url=args.camel_p_llm_base_url,
            p_llm_model=args.camel_p_llm_model,
            p_llm_api_key=args.camel_p_llm_api_key,
            vlm_base_url=args.camel_vlm_base_url,
            vlm_model=args.camel_vlm_model,
            vlm_api_key=args.camel_vlm_api_key,
            q_llm_base_url=args.camel_q_llm_base_url,
            q_llm_model=args.camel_q_llm_model,
            q_llm_second_model=args.camel_q_llm_second_model,
            q_llm_api_key=args.camel_q_llm_api_key,
            model_defense_level=args.camel_model_defense_level,
            pllm_exposure_level=args.camel_pllm_exposure_level,
            system_defense_level=args.camel_system_defense_level,
            replan=args.camel_replan,
            plan_only=args.camel_plan_only,
            vlm_use_relative_coords=args.camel_vlm_use_relative_coords,
        )
        agent_type = "CamelAgent"
    elif args.agent_type == "PromptAgent":
        agent = PromptAgent(
            model=args.model,
            max_tokens=args.max_tokens,
            top_p=args.top_p,
            temperature=args.temperature,
            action_space=args.action_space,
            observation_type=args.observation_type,
            max_trajectory_length=args.max_trajectory_length,
        )
        agent_type = args.agent_type
    elif args.agent_type == "FidesAgent":
        model_type = "qwen25vl"
        coordinate_type = (
            "absolute"
            if args.observation_type == "screenshot_a11y_tree"
            else "relative"
        )
        agent = FidesOpenCUAAgent(
            action_space=args.action_space,
            observation_type=args.observation_type,
            max_image_history_length=MAX_IMAGE_HISTORY_LENGTH,
            model_type=model_type,
            port=os.getenv("VLLM_PORT", "8001"),
            model=args.model,
            history_type="action_history",
            coordinate_type=coordinate_type,
            max_tokens=8192,
        )
        agent_type = args.agent_type
    elif args.agent_type == "HybridAgent":
        agent = HybridAgent(
            model=args.model,
            max_tokens=args.max_tokens,
            top_p=args.top_p,
            temperature=args.temperature,
            action_space=args.action_space,
            observation_type=args.observation_type,
            max_trajectory_length=args.max_trajectory_length,
        )
        agent_type = args.agent_type
    elif args.agent_type == "QwenAgent":
        from mm_agents.qwen_agent import QwenAgent

        agent = QwenAgent(
            model=args.model,
            max_tokens=args.max_tokens,
            top_p=args.top_p,
            temperature=args.temperature,
            action_space=args.action_space,
            observation_type=args.observation_type,
            base_url=os.getenv("QWEN_BASE_URL", "http://localhost:8000/v1"),
            api_key=os.getenv("QWEN_API_KEY", None),
        )
        agent_type = args.agent_type
    elif args.agent_type == "UCMAgent":
        from mm_agents.ucm_agent.agent import UCMAgent

        agent = UCMAgent(
            model=args.ucm_model or args.model,
            allow_unsolvable=args.ucm_allow_unsolvable,
            max_tokens=args.max_tokens,
        )
        agent_type = args.agent_type
    elif args.agent_type == "HumanAgent":
        agent = HumanAgent(
            action_space=args.action_space,
            observation_type=args.observation_type,
            screen_width=args.screen_width,
            screen_height=args.screen_height,
        )
        agent_type = args.agent_type
    elif args.agent_type == "ClaudeCUAAgent":
        from mm_agents.anthropic.claude_cua_agent import ClaudeCUAAgent

        agent = ClaudeCUAAgent(
            model=args.model,
            provider=args.api_provider,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            action_space=args.action_space,
            screen_width=args.screen_width,
            screen_height=args.screen_height,
            no_thinking=args.no_thinking,
            use_isp=args.use_isp,
            effort=args.effort,
            max_steps=max_steps,
        )
        agent_type = args.agent_type
    else:
        raise ValueError(f"Unknown agent type: {args.agent_type}")

    env = DesktopEnv(
        provider_name=args.provider_name,
        region=args.region,
        path_to_vm=args.path_to_vm,
        action_space=agent.action_space,
        snapshot_name=args.snapshot_name,
        screen_size=(args.screen_width, args.screen_height),
        headless=args.headless,
        os_type="Ubuntu",
        require_a11y_tree=args.observation_type
        in ["a11y_tree", "screenshot_a11y_tree", "som"],
        aws_ami=args.aws_ami,
        ucm_reveal=args.ucm_reveal,
    )

    if agent_type == "SA-Agent":
        from mm_agents.sa_agent.loop import SAAgentLoop

        loop = SAAgentLoop(
            agent, env, example_result_dir="", max_steps=max_steps, args=args
        )
    elif agent_type == "CamelAgent":
        from mm_agents.camel_agent.loop import CamelAgentLoop

        loop = CamelAgentLoop(
            agent, env, example_result_dir="", max_steps=max_steps, args=args
        )
    elif agent_type in ("PromptAgent", "QwenAgent", "ClaudeCUAAgent"):
        loop = PromptAgentLoop(
            agent, env, example_result_dir="", max_steps=max_steps, args=args
        )
    elif agent_type == "HybridAgent":
        loop = HybridAgentLoop(
            agent, env, example_result_dir="", max_steps=max_steps, args=args
        )
    elif agent_type == "HumanAgent":
        loop = HumanAgentLoop(
            agent, env, example_result_dir="", max_steps=max_steps, args=args
        )
    elif agent_type == "FidesAgent":
        from mm_agents.fides.loop import FidesOpenCUALoop

        loop = FidesOpenCUALoop(
            agent,
            env,
            example_result_dir="",
            max_steps=max_steps,
            args=args,
            variant=args.fides_variant,
            p_llm=args.fides_p_llm,
            q_llm=args.fides_q_llm,
            q_llm_second_check=args.fides_q_llm_second_check,
            system_defense_level=args.fides_system_defense_level,
            model_defense_level=args.fides_model_defense_level,
            token_log_path=args.fides_token_log_path,
        )
    elif agent_type == "UCMAgent":
        from mm_agents.ucm_agent.loop import UCMLoop
        loop = UCMLoop(
            agent,
            env,
            example_result_dir="",
            max_steps=max_steps,
            args=args,
            qllm_model=args.ucm_qllm_model,
            site_hint=args.ucm_site,
        )
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")

    for domain in tqdm(test_all_meta, desc="Domain"):
        for example_id in tqdm(test_all_meta[domain], desc="Example", leave=False):
            config_file = os.path.join(
                args.test_config_base_dir, f"examples/{domain}/{example_id}.json"
            )
            with open(config_file, "r", encoding="utf-8") as f:
                example = json.load(f)

            example["domain"] = domain

            instruction = example["instruction"]

            logger.info(f"[Domain]: {domain}")
            logger.info(f"[Example ID]: {example_id}")
            logger.info(f"[Instruction]: {instruction}")
            cfg_args["instruction"] = instruction
            cfg_args["start_time"] = datetime.datetime.now().strftime(
                "%Y:%m:%d-%H:%M:%S"
            )

            example_result_dir = os.path.join(
                args.result_dir,
                args.action_space,
                args.observation_type,
                args.run_label,
                domain,
                example_id,
            )
            os.makedirs(example_result_dir, exist_ok=True)
            try:
                lib_run_single.run_single_example(
                    loop,
                    env,
                    example,
                    instruction,
                    example_result_dir,
                    scores,
                    args,
                )
            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(f"Exception in {domain}/{example_id}: {e}\n{tb_str}")

                if env.provider_name == "aws":
                    env.terminate()

                abs_result_dir = os.path.abspath(example_result_dir)
                try:
                    os.makedirs(abs_result_dir, exist_ok=True)
                except OSError:
                    pass
                try:
                    env.controller.end_recording(
                        os.path.join(abs_result_dir, "recording.mp4")
                    )
                except Exception as rec_e:
                    logger.error(f"Could not save recording for {domain}/{example_id}: {rec_e}")
                try:
                    with open(os.path.join(abs_result_dir, "traj.jsonl"), "a") as f:
                        f.write(
                            json.dumps(
                                {"Error": f"{e} --- {domain}/{example_id}", "traceback": tb_str}
                            )
                        )
                        f.write("\n")
                except OSError as io_e:
                    logger.error(f"Could not write traj.jsonl for {domain}/{example_id}: {io_e}")

            if env.provider_name == "aws":
                env.terminate()

    if env.provider_name != "aws":
        env.close()


def get_unfinished(
    action_space,
    use_model,
    observation_type,
    result_dir,
    total_file_json,
    preserve_plan_code=False,
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
                    if "result.txt" not in os.listdir(
                        example_path
                    ) or "adversary_result.txt" not in os.listdir(example_path):
                        if example_id not in total_file_json.get(domain, []):
                            continue
                        for file in os.listdir(example_path):
                            file_path = os.path.join(example_path, file)
                            if preserve_plan_code and file == "plan_code.txt":
                                continue
                            if os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                            else:
                                os.remove(file_path)
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


def _register_token_roles(args: argparse.Namespace) -> None:
    """Tell the token accounting which model plays which role in this run.

    Agents whose several models are resolved inside their own loop (SA-Agent, FidesAgent,
    UCMAgent) register there, where the defaults are applied, so the mapping cannot drift
    from what the run used. The rest are resolved straight from the flags and registered
    here. A role whose model is unset is skipped; two roles sharing one model report as one
    joined bucket ("planner+q_llm"), which is what a single-model run actually is.
    """
    if args.agent_type == "CamelAgent":
        token_usage.register_role("planner", args.camel_p_llm_model)
        token_usage.register_role("vlm", args.camel_vlm_model)
        token_usage.register_role("q_llm", args.camel_q_llm_model)
        token_usage.register_role("q_llm_second", args.camel_q_llm_second_model)
    elif args.agent_type in ("PromptAgent", "QwenAgent", "HybridAgent"):
        token_usage.register_role("executor", args.model)


def print_scores(args: argparse.Namespace) -> None:
    """Read finished result files and print task/adversary success counts."""
    target_dir = os.path.join(
        args.result_dir, args.action_space, args.observation_type, args.run_label
    )
    if not os.path.exists(target_dir):
        print(f"No results found at: {target_dir}")
        return

    domain_stats: dict[str, dict] = {}
    role_models: dict[str, str] = {}

    for domain in sorted(os.listdir(target_dir)):
        domain_path = os.path.join(target_dir, domain)
        if not os.path.isdir(domain_path):
            continue
        results, adv_results, token_totals = [], [], []
        by_model: dict[str, dict] = {}
        by_role: dict[str, dict] = {}
        for example_id in os.listdir(domain_path):
            example_path = os.path.join(domain_path, example_id)
            if not os.path.isdir(example_path):
                continue
            r_path = os.path.join(example_path, "result.txt")
            a_path = os.path.join(example_path, "adversary_result.txt")
            if os.path.exists(r_path):
                try:
                    results.append(float(open(r_path).read().strip()))
                except ValueError:
                    pass
            if os.path.exists(a_path):
                try:
                    adv_results.append(float(open(a_path).read().strip()))
                except ValueError:
                    pass
            episode_tokens = token_usage.load_episode_total(example_path)
            if episode_tokens:
                token_totals.append(episode_tokens)
                for key, dest in (("by_model", by_model), ("by_role", by_role)):
                    for name, counters in token_usage.load_episode_breakdown(example_path, key).items():
                        bucket = dest.setdefault(name, {"episodes": 0})
                        bucket["episodes"] += 1
                        for field, value in counters.items():
                            if isinstance(value, (int, float)):
                                bucket[field] = bucket.get(field, 0) + value
                role_models.update(token_usage.load_role_models(example_path))
        if results or adv_results:
            domain_stats[domain] = {
                "results": results,
                "adv_results": adv_results,
                "tokens": token_totals,
                "by_model": by_model,
                "by_role": by_role,
            }

    if not domain_stats:
        print(f"No finished results found under: {target_dir}")
        return

    all_results = [v for d in domain_stats.values() for v in d["results"]]
    all_adv = [v for d in domain_stats.values() for v in d["adv_results"]]

    print(f"\n=== Scores for: {args.run_label} ===")
    print(f"Results dir: {target_dir}\n")

    col = 24
    print(f"{'Domain':<{col}}  {'Task':>12}  {'Adversary':>12}")
    print("-" * (col + 28))
    for domain, stats in domain_stats.items():
        r = stats["results"]
        a = stats["adv_results"]
        r_str = f"{sum(r):.0f}/{len(r)} ({100 * sum(r) / len(r):.1f}%)" if r else "—"
        a_str = f"{sum(a):.0f}/{len(a)} ({100 * sum(a) / len(a):.1f}%)" if a else "—"
        print(f"{domain:<{col}}  {r_str:>12}  {a_str:>12}")

    print("-" * (col + 28))
    r_str = (
        f"{sum(all_results):.0f}/{len(all_results)} ({100 * sum(all_results) / len(all_results):.1f}%)"
        if all_results
        else "—"
    )
    a_str = (
        f"{sum(all_adv):.0f}/{len(all_adv)} ({100 * sum(all_adv) / len(all_adv):.1f}%)"
        if all_adv
        else "—"
    )
    print(f"{'TOTAL':<{col}}  {r_str:>12}  {a_str:>12}")
    print()

    _print_token_scores(domain_stats, col)
    _print_token_scores_by_model(domain_stats, role_models, col)


def _print_token_scores(domain_stats: dict, col: int) -> None:
    """Per-domain token consumption, read from each episode's token_usage.json.

    Averages are per *episode*, over the episodes that actually carry token accounting --
    runs recorded before token accounting existed simply show no rows here.
    """
    all_tokens = [t for d in domain_stats.values() for t in d["tokens"]]
    if not all_tokens:
        return

    def _mean(totals: list, key: str) -> float:
        return sum(t.get(key, 0) for t in totals) / len(totals) if totals else 0.0

    print(f"{'Domain':<{col}}  {'Episodes':>8}  {'Calls':>7}  {'Input':>12}  "
          f"{'Output':>10}  {'Reasoning':>10}  {'Total':>12}")
    print("-" * (col + 68))
    rows = [(d, s["tokens"]) for d, s in domain_stats.items() if s["tokens"]]
    for domain, totals in rows + [("TOTAL (mean/episode)", all_tokens)]:
        if domain.startswith("TOTAL"):
            print("-" * (col + 68))
        print(
            f"{domain:<{col}}  {len(totals):>8}  {_mean(totals, 'calls'):>7.1f}  "
            f"{_mean(totals, 'input_tokens'):>12,.0f}  {_mean(totals, 'output_tokens'):>10,.0f}  "
            f"{_mean(totals, 'reasoning_tokens'):>10,.0f}  {_mean(totals, 'total_tokens'):>12,.0f}"
        )
    grand = sum(t.get("total_tokens", 0) for t in all_tokens)
    print(f"\nSum over all {len(all_tokens)} accounted episodes: {grand:,.0f} tokens "
          f"({sum(t.get('input_tokens', 0) for t in all_tokens):,.0f} in / "
          f"{sum(t.get('output_tokens', 0) for t in all_tokens):,.0f} out)")
    print()


def _print_token_scores_by_model(domain_stats: dict, role_models: dict, col: int) -> None:
    """Token consumption split by model, and by the role each model played.

    This is what separates a dual-LLM run's models: when e.g. --sa_p_llm and --sa_q_llm
    name different models, each gets its own row here, while the table above still shows the
    combined per-episode totals. Sums (not per-episode means) -- the roles of one run do not
    all fire the same number of times per episode, so a mean would not be comparable across
    rows. A single-model run collapses to one row per table, which is the honest reading:
    the SDK call carries nothing that would split one model's calls between two roles, so
    roles sharing a model are reported as one joined bucket ("planner+q_llm").
    """
    def _merge(key: str) -> dict:
        merged: dict[str, dict] = {}
        for stats in domain_stats.values():
            for name, counters in stats.get(key, {}).items():
                bucket = merged.setdefault(name, {})
                for field, value in counters.items():
                    bucket[field] = bucket.get(field, 0) + value
        return merged

    def _table(title: str, merged: dict, label_of=lambda name: name) -> None:
        if not merged:
            return
        labels = {name: label_of(name) for name in merged}
        width = max([col, len(title)] + [len(v) for v in labels.values()])
        print(f"{title:<{width}}  {'Episodes':>8}  {'Calls':>7}  {'Input':>12}  "
              f"{'Output':>10}  {'Reasoning':>10}  {'Total':>12}")
        print("-" * (width + 68))
        for name in sorted(merged, key=lambda n: -merged[n].get("total_tokens", 0)):
            b = merged[name]
            print(f"{labels[name]:<{width}}  {b.get('episodes', 0):>8}  {b.get('calls', 0):>7,.0f}  "
                  f"{b.get('input_tokens', 0):>12,.0f}  {b.get('output_tokens', 0):>10,.0f}  "
                  f"{b.get('reasoning_tokens', 0):>10,.0f}  {b.get('total_tokens', 0):>12,.0f}")
        print()

    _table("Model (sum)", _merge("by_model"))
    roles = _merge("by_role")
    _table("Role (sum)", roles,
           label_of=lambda r: f"{r} [{role_models[r]}]" if r in role_models else r)


if __name__ == "__main__":
    os.environ["RESET_REDDIT_DATABASE"] = "True"

    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    args = config()

    if args.score:
        print_scores(args)
        sys.exit(0)

    token_usage.enforce(args.model)
    _register_token_roles(args)

    openrouter.configure(args.max_trajectory_length)

    with open(args.test_all_meta_path, "r", encoding="utf-8") as f:
        test_all_meta = json.load(f)

    if args.domain != "all":
        test_all_meta = {args.domain: test_all_meta[args.domain]}

    if args.task_ids is not None:
        ids = {t.strip() for t in args.task_ids.split(",")}
        test_all_meta = {
            d: [t for t in tasks if t in ids] for d, tasks in test_all_meta.items()
        }
        test_all_meta = {d: tasks for d, tasks in test_all_meta.items() if tasks}

    if args.task_filter is not None:
        test_all_meta = {
            d: [t for t in tasks if args.task_filter in t]
            for d, tasks in test_all_meta.items()
        }
        test_all_meta = {d: tasks for d, tasks in test_all_meta.items() if tasks}

    test_file_list = (
        test_all_meta
        if args.again
        else get_unfinished(
            args.action_space,
            args.run_label,
            args.observation_type,
            args.result_dir,
            test_all_meta,
            preserve_plan_code=not getattr(args, "camel_replan", False),
        )
    )
    left_info = ""

    total_number = 0
    for domain in test_file_list:
        left_info += f"{domain}: {len(test_file_list[domain])}\n"
        total_number += len(test_file_list[domain])
    logger.info(f"Left tasks:\n{left_info}")

    if total_number == 0:
        sys.exit(0)

    if any("rocketchat" in d and tasks for d, tasks in test_file_list.items()):
        from adv.TheAgentCompany_RocketChat.npc_model import check_npc_model
        try:
            logger.info(f"RocketChat NPC model: {check_npc_model()}")
        except RuntimeError as e:
            logger.error(f"Refusing to start: {e}")
            sys.exit(1)

    get_result(
        args.action_space,
        args.run_label,
        args.observation_type,
        args.result_dir,
        test_all_meta,
    )
    test(args, test_file_list)
