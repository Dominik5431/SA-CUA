"""
!!! Dead reference copy from mm_agents/fides — not runnable (bare local imports),
kept only for reference. The live loop is in loop.py.
"""

import datetime
import io
import json
import logging
import os
import time
import PIL
from wrapt_timeout_decorator import *
from loop_and_planner import LabeledPlanningLoop
from lattice import Lattice, ProductLabel
from openai.types.chat import ChatCompletionUserMessageParam

logger = logging.getLogger("desktopenv.experiment")


def run_single_example(
            loop : LabeledPlanningLoop,
            initial_label : ProductLabel,
            example,
            instruction,
            example_result_dir,
            scores
        ):
    runtime_logger, fh, attached = setup_logger(example, example_result_dir)
    loop.agent.logger = runtime_logger
    loop.agent.reset(runtime_logger)
    try:
        
        loop.env.reset(task_config=example)
        time.sleep(30)
        obs = loop.env._get_obs()
        loop.env.controller.start_recording()

        raw = loop.env.controller.get_screenshot()
        img = PIL.Image.open(io.BytesIO(raw))
        target = f"{example_result_dir}/step_0.png"
        img.save(target)
        logger.info(f"Saved screenshot to {target}")

        loop.turn = 0
        loop.step_idx = 0

        user_message = ChatCompletionUserMessageParam(role = "user", content = instruction)

        loop.loop(user_message, label=initial_label)

        result = loop.env.evaluate()
        logger.info("Result: %.2f", result)
        scores.append(result)
        with open(os.path.join(example_result_dir, "result.txt"), "w", encoding="utf-8") as f:
            f.write(f"{result}\n")
        loop.env.controller.end_recording(os.path.join(example_result_dir, "recording.mp4"))
    finally:
        for lg in attached: 
            lg.removeHandler(fh)
        runtime_logger.removeHandler(fh)
        fh.close()


def setup_logger(example, example_result_dir):
    """
    Create a per-example logger whose file handler is *detached* once the
    episode finishes (see run_single_example).
    """
    example_name   = f"desktopenv.example.{example['id']}"
    runtime_logger = logging.getLogger(example_name)
    runtime_logger.setLevel(logging.DEBUG)
    runtime_logger.propagate = False

    fh = logging.FileHandler(os.path.join(example_result_dir, "runtime.log"))
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    runtime_logger.addHandler(fh)

    attached_to = []
    for module_name, level in [
        ("desktopenv.agent",       logging.DEBUG),
        ("desktopenv.env",         logging.INFO),
        ("desktopenv.experiment",  logging.INFO),
    ]:
        lg = logging.getLogger(module_name)
        lg.setLevel(level)
        lg.propagate = False
        lg.addHandler(fh)
        attached_to.append(lg)

    return runtime_logger, fh, attached_to