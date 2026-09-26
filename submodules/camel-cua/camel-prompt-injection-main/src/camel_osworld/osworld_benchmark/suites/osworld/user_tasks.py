from pathlib import Path
from types import SimpleNamespace
import json
import logging
from typing import Dict, Any
import itertools

import osworld
from agentdojo.base_tasks import BaseUserTask, TaskDifficulty
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.task_suite import TaskSuite

from camel_osworld.osworld_benchmark.suites.osworld.base_ui_task_suite_uitars import (
    build_ui, UIEnvironment,
) # task_suite, 
from camel_osworld.osworld_benchmark.suites.osworld.base_ui_task_suite_opencua import (
    build_ui_opencua, UIEnvironment_OpenCUA,
)
from camel_osworld.osworld_benchmark.suites.osworld.base_ui_task_suite_anthropic import (
    build_ui_anthropic, UIEnvironment_Anthropic,
)
UIEnv = UIEnvironment|UIEnvironment_OpenCUA|UIEnvironment_Anthropic

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PKG_ROOT = Path(osworld.__file__).parent
EXAMPLES_ROOT = PKG_ROOT / "evaluation_examples" / "examples"

# Public mappings for CLI translation/debug
TASK_NAME_MAP: dict[str, str] = {}    # CLI → class name (debug only)
CLASS_TO_FILE: dict[str, Path] = {}   # class name -> JSON path
CLI_TO_SUITE_ID: dict[str, str] = {}  # CLI → suite id (what benchmark() expects)
SUITE_ID_TO_CLI: dict[str, str] = {}   # e.g., "user_task_8" -> "chrome/2ad9..."
SUITE_ID_TO_FILE: dict[str, Path] = {} # e.g., "user_task_8" -> /abs/path/to/json

class UIUserTask(BaseUserTask[UIEnv]):
    """Base class for OSWorld tasks backed by a JSON example file."""
    task_file: Path
    model_defense_level: int = 3  # default; can be overridden per task
    q_llm: str | None = None
    q_llm_second_check: str | None = None
    token_count_file: str | None = None
    path_to_vlm: str | None = None
    path_to_vm: str | None = None
    port: int = 15001
    plan_analysis_only: bool = False

    def __init__(self) -> None:
        super().__init__()
        with open(self.task_file, "r", encoding="utf-8") as f:
            self.example: Dict[str, Any] = json.load(f)
        self.PROMPT = self.example["instruction"]
    
    def _select_builder_from_path(self, path_to_vlm: str | None):
        """
        Return the appropriate UI builder based on a simple, case-insensitive
        substring check on `path_to_vlm`:
        - contains 'tars'   -> build_ui        (UITARS/TARS)
        - contains 'opencua'-> build_ui_opencua
        - contains 'claude' -> build_ui_anthropic
        - default           -> build_ui_opencua
        """
        p = (path_to_vlm or "").lower()
        if "tars" in p:
            return build_ui
        if "opencua" in p:
            return build_ui_opencua
        if "claude" in p:
            return build_ui_anthropic
        return build_ui_opencua


    def init_environment(self, env: UIEnv) -> UIEnv:
        logger.info(f"[{self.__class__.__name__}] Initializing environment with: {self.task_file}")

        # Plan-analysis-only: lightweight stub, no VM/VLM connection needed
        if self.plan_analysis_only:
            logger.info(f"[{self.__class__.__name__}] plan_analysis_only=True — skipping VM setup")
            stub = SimpleNamespace(
                ID=getattr(self, "ID", self.task_file.stem),
                OSW_CLI=getattr(self, "OSW_CLI", None),
                task_config=self.example,
                path=None,
                token_count_file=self.token_count_file,
                env=None,  # no DesktopEnv
            )
            env.base_ui = stub
            return env

        # Choose the correct builder based on path_to_vlm
        build = self._select_builder_from_path(self.path_to_vlm)
        p = (self.path_to_vlm or "").lower()
        is_claude = "claude" in p
        kwargs = dict(
            defense_level=self.model_defense_level,
            q_llm=self.q_llm,
            q_llm_second_check=self.q_llm_second_check,
            path_to_vlm=self.path_to_vlm,
            path_to_vm=self.path_to_vm,
            token_count_file=self.token_count_file
        )
        if not is_claude:
            # build_ui / build_ui_opencua need port; build_ui_anthropic does not
            kwargs["port"] = self.port

        ui = build(**kwargs)
        
        try:
            ui.env.reset(task_config=self.example)
        except Exception as e:
            logger.error(f"Failed to reset environment: {e}")
            raise RuntimeError(f"Failed to reset environment with task config: {self.example}") from e
        env.base_ui = ui
        env.base_ui.ID = getattr(self, "ID", self.task_file.stem)
        env.base_ui.OSW_CLI = getattr(self, "OSW_CLI", None)           # e.g. "chrome/uuid"

        env.base_ui.task_config = self.example
        return env

    # Empty ground truth is fine per your requirements
    def ground_truth(self, pre_environment: UIEnv) -> list[FunctionCall]:
        return []

    def utility(
        self,
        model_output: str,
        pre_environment: UIEnv,
        post_environment: UIEnv,
        strict: bool = True,
    ) -> bool:
        logger.info(f"[{self.__class__.__name__}] Evaluating via DesktopEnv.evaluate()")
        env = getattr(post_environment, "base_ui", None)
        if not env or not getattr(env, "env", None):
            logger.error("No base UI environment found in post_environment.")
            return False
        try:
            score = env.env.evaluate()
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return False
        return float(score) == 1.0


def _is_task_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("instruction"), str)


def _cli_name_for(json_path: Path) -> str:
    """
    Build CLI name including app subfolder, e.g. 'chrome/<stem>'.
    If deeper nested, still take the first folder under EXAMPLES_ROOT.
    """
    rel = json_path.relative_to(EXAMPLES_ROOT)
    parts = rel.parts
    app = parts[0].lower() if len(parts) >= 2 else "misc"
    stem = json_path.stem
    return f"{app}/{stem}"


def scan_and_register(root: Path, task_suite: TaskSuite) -> dict[str, str]:
    if not root.exists():
        logger.warning(f"OSWorld examples path not found: {root}")
        return {}

    json_paths = sorted(
        (p for p in root.rglob("*.json") if _is_task_json(p)),
        key=lambda p: str(p.relative_to(root))
    )
    
    # Build a stable mapping: task_id -> numeric index
    task_id_to_index = {}
    all_task_ids = []
    
    # First pass: collect all task IDs in sorted order
    for jp in json_paths:
        try:
            obj = json.loads(jp.read_text(encoding="utf-8"))
            task_id = obj.get("id") or obj.get("ID") or _cli_name_for(jp)
            all_task_ids.append((task_id, jp))
        except Exception as e:
            logger.warning(f"Skipping {jp} (invalid JSON): {e}")
    
    # Sort by task_id to ensure deterministic ordering
    all_task_ids.sort(key=lambda x: x[0])
    
    # Assign stable indices
    for idx, (task_id, jp) in enumerate(all_task_ids):
        task_id_to_index[str(jp)] = idx
    
    # Second pass: register with stable indices
    for jp in json_paths:
        cli_name = _cli_name_for(jp)
        
        try:
            obj = json.loads(jp.read_text(encoding="utf-8"))
        except Exception as e:
            continue
        
        task_id = obj.get("id") or obj.get("ID") or cli_name
        
        # Use the stable index based on task_id sort order
        idx = task_id_to_index[str(jp)]
        class_name = f"UserTask{idx}"
        suite_id = f"user_task_{idx}"

        cls = type(
            class_name,
            (UIUserTask,),
            {
                "DIFFICULTY": TaskDifficulty.MEDIUM,
                "ID": task_id,
                "task_file": jp,
                "OSW_CLI": cli_name,
                "__doc__": f"Auto-registered OSWorld task from {jp}",
            },
        )

        task_suite.register_user_task(cls)

        # Mappings
        TASK_NAME_MAP[cli_name] = class_name
        TASK_NAME_MAP.setdefault(jp.stem, class_name)
        TASK_NAME_MAP.setdefault(f"{cli_name.split('/')[0]}_{jp.stem}", class_name)

        CLI_TO_SUITE_ID[cli_name] = suite_id
        CLI_TO_SUITE_ID.setdefault(jp.stem, suite_id)
        CLI_TO_SUITE_ID.setdefault(f"{cli_name.split('/')[0]}_{jp.stem}", suite_id)
        SUITE_ID_TO_CLI[suite_id] = cli_name
        SUITE_ID_TO_FILE[suite_id] = jp
        CLASS_TO_FILE[class_name] = jp

    return CLI_TO_SUITE_ID
# run at import
# _scan_and_register(EXAMPLES_ROOT)

__all__ = [
  "UIUserTask", "TASK_NAME_MAP", "CLASS_TO_FILE",
  "CLI_TO_SUITE_ID", "SUITE_ID_TO_CLI", "SUITE_ID_TO_FILE"
]
