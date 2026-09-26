from pathlib import Path
from types import SimpleNamespace
import json
import logging
from typing import Dict, Any

from agentdojo.base_tasks import BaseUserTask, TaskDifficulty
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.task_suite import TaskSuite

from camel_osworld.osworld_benchmark.suites.osworld.user_tasks import UIUserTask, UIEnv
from camel_osworld.osworld_benchmark.suites.redteamcua.build_ui import (
    build_ui_redteamcua_vllm,
    build_ui_redteamcua_anthropic,
)

logger = logging.getLogger(__name__)

import desktop_env as _desktop_env_pkg
REDTEAMCUA_ROOT = Path(_desktop_env_pkg.__file__).resolve().parent.parent
REDTEAMCUA_EXAMPLES_ROOT = REDTEAMCUA_ROOT / "evaluation_examples" / "examples"


def _resolve_upload_paths(example: dict) -> dict:
    """Make file paths in adversary_config absolute relative to REDTEAMCUA_ROOT."""
    def _abs(params: dict, keys: tuple) -> dict:
        params = dict(params)
        for key in keys:
            val = params.get(key)
            if val and not Path(val).is_absolute():
                params[key] = str(REDTEAMCUA_ROOT / val)
        return params

    resolved = []
    for step in example.get("adversary_config", []):
        if step.get("type") == "owncloud_adv_setup":
            step = {**step, "parameters": _abs(step.get("parameters", {}), ("local_file_path",))}
        resolved.append(step)

    return {**example, "adversary_config": resolved} if resolved else example

ADVERSARY_SCORES: dict[str, float] = {}

# Stable index mappings (parallel to osworld's maps)
TASK_NAME_MAP: dict[str, str] = {}
CLASS_TO_FILE: dict[str, Path] = {}
CLI_TO_SUITE_ID: dict[str, str] = {}
SUITE_ID_TO_CLI: dict[str, str] = {}
SUITE_ID_TO_FILE: dict[str, Path] = {}


class RedTeamCUAUserTask(UIUserTask):
    """Base class for RedTeamCUA tasks backed by a JSON example file."""

    vllm_port: int = 8000
    vllm_model: str = "Qwen/Qwen3.5-9B"

    def __init__(self) -> None:
        super().__init__()
        self.example = _resolve_upload_paths(self.example)

    def _select_builder_from_path(self, path_to_vlm: str | None):
        p = (path_to_vlm or "").lower()
        if "claude" in p:
            return build_ui_redteamcua_anthropic
        return build_ui_redteamcua_vllm

    def init_environment(self, env: UIEnv) -> UIEnv:
        logger.info(f"[{self.__class__.__name__}] Initializing RedTeamCUA env: {self.task_file}")

        if self.plan_analysis_only:
            stub = SimpleNamespace(
                ID=getattr(self, "ID", self.task_file.stem),
                OSW_CLI=getattr(self, "OSW_CLI", None),
                task_config=self.example,
                path=None,
                token_count_file=self.token_count_file,
                env=None,
            )
            env.base_ui = stub
            return env

        build = self._select_builder_from_path(self.path_to_vlm)
        p = (self.path_to_vlm or "").lower()
        is_claude = "claude" in p

        if is_claude:
            kwargs: Dict[str, Any] = dict(
                defense_level=self.model_defense_level,
                q_llm=self.q_llm,
                q_llm_second_check=self.q_llm_second_check,
                path_to_vlm=self.path_to_vlm,
                path_to_vm=self.path_to_vm,
                token_count_file=self.token_count_file,
            )
        else:
            kwargs = dict(
                vllm_port=self.vllm_port,
                vllm_model=self.vllm_model,
                defense_level=self.model_defense_level,
                q_llm=self.q_llm,
                q_llm_second_check=self.q_llm_second_check,
                path_to_vm=self.path_to_vm,
                token_count_file=self.token_count_file,
            )

        ui = build(**kwargs)

        try:
            # RedTeamCUA uses prepare_injection() (not reset()) to start the VM
            # snapshot, apply task config, and inject the adversarial payload.
            ui.env.prepare_injection(task_config=self.example)
        except Exception as e:
            logger.error(f"[{self.__class__.__name__}] Environment setup failed for {self.task_file.name}: {e}")
            try:
                ui.env.close()
            except Exception:
                pass
            # Return env with base_ui=None so utility() returns False without crashing the benchmark.
            env.base_ui = None
            return env

        env.base_ui = ui
        env.base_ui.ID = getattr(self, "ID", self.task_file.stem)
        env.base_ui.OSW_CLI = getattr(self, "OSW_CLI", None)
        env.base_ui.task_config = self.example
        return env

    def utility(
        self,
        model_output: str,
        pre_environment: UIEnv,
        post_environment: UIEnv,
        strict: bool = True,
    ) -> bool:
        logger.info(f"[{self.__class__.__name__}] Evaluating RedTeamCUA task")
        env = getattr(post_environment, "base_ui", None)
        if not env or not getattr(env, "env", None):
            logger.error("No base UI environment found in post_environment.")
            return False

        try:
            score = env.env.evaluate("")
        except Exception as e:
            logger.error(f"Task evaluation failed: {e}")
            score = 0

        try:
            adv_score = env.env.adversary_evaluate()
            ADVERSARY_SCORES[self.ID] = float(adv_score)
            logger.info(f"[{self.__class__.__name__}] Adversary score: {adv_score}")
        except Exception as e:
            logger.warning(f"Adversary evaluation failed: {e}")

        return float(score) == 1.0


def _is_task_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("instruction"), str)


def _cli_name_for(json_path: Path) -> str:
    rel = json_path.relative_to(REDTEAMCUA_EXAMPLES_ROOT)
    parts = rel.parts
    app = parts[0].lower() if len(parts) >= 2 else "misc"
    return f"{app}/{json_path.stem}"


def scan_and_register_redteamcua(task_suite: TaskSuite) -> dict[str, str]:
    root = REDTEAMCUA_EXAMPLES_ROOT
    if not root.exists():
        logger.warning(f"RedTeamCUA examples path not found: {root}")
        return {}

    json_paths = sorted(
        (p for p in root.rglob("*.json") if _is_task_json(p)),
        key=lambda p: str(p.relative_to(root)),
    )

    all_task_ids = []
    for jp in json_paths:
        try:
            obj = json.loads(jp.read_text(encoding="utf-8"))
            task_id = obj.get("id") or obj.get("ID") or _cli_name_for(jp)
            all_task_ids.append((task_id, jp))
        except Exception as e:
            logger.warning(f"Skipping {jp}: {e}")

    all_task_ids.sort(key=lambda x: x[0])
    task_id_to_index = {str(jp): idx for idx, (_, jp) in enumerate(all_task_ids)}

    for jp in json_paths:
        cli_name = _cli_name_for(jp)
        try:
            obj = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue

        task_id = obj.get("id") or obj.get("ID") or cli_name
        idx = task_id_to_index[str(jp)]
        class_name = f"UserTask{idx}"
        suite_id = f"user_task_{idx}"

        cls = type(
            class_name,
            (RedTeamCUAUserTask,),
            {
                "DIFFICULTY": TaskDifficulty.MEDIUM,
                "ID": task_id,
                "task_file": jp,
                "OSW_CLI": cli_name,
                "__doc__": f"Auto-registered RedTeamCUA task from {jp}",
            },
        )

        task_suite.register_user_task(cls)

        TASK_NAME_MAP[cli_name] = class_name
        TASK_NAME_MAP.setdefault(jp.stem, class_name)

        CLI_TO_SUITE_ID[cli_name] = suite_id
        CLI_TO_SUITE_ID.setdefault(jp.stem, suite_id)
        SUITE_ID_TO_CLI[suite_id] = cli_name
        SUITE_ID_TO_FILE[suite_id] = jp
        CLASS_TO_FILE[class_name] = jp

    return CLI_TO_SUITE_ID


__all__ = [
    "RedTeamCUAUserTask",
    "ADVERSARY_SCORES",
    "REDTEAMCUA_EXAMPLES_ROOT",
    "TASK_NAME_MAP",
    "CLASS_TO_FILE",
    "CLI_TO_SUITE_ID",
    "SUITE_ID_TO_CLI",
    "SUITE_ID_TO_FILE",
    "scan_and_register_redteamcua",
]
