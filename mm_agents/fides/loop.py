from typing import Literal, Optional

from openai.types.chat import ChatCompletionUserMessageParam

from mm_agents import token_usage
from mm_agents.utils.loop import Loop
from mm_agents.fides.tool_scheme import Tool
from mm_agents.fides.lattice import IntegrityLabel, InverseLattice, PowersetLattice, ProductLabel
from mm_agents.fides.qllm import QLLMAgent
from mm_agents.fides.prompts import (
    SYST_PROMPT_PLLM_FULL_EXPOSURE,
    SYST_PROMPT_PLLM_NO_EXPOSURE,
    build_pllm_prompt,
    SYST_DEF_1,
    SYST_DEF_2,
    SYST_DEF_3,
)
from mm_agents.fides.tools import (
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
)


def _as_planner_model(model_str: str) -> str:
    """
    Normalize a model string to the '<provider>:<model_name>' format expected by
    the Fides P-LLM (see get_tool_response in loop_and_planner*.py).

    --model (and other agents' *_model args) follow this repo's own convention of
    '<provider> | <model_name>' instead, so when --fides_p_llm isn't given and we
    fall back to --model, translate the delimiter rather than passing it through.
    """
    if ":" in model_str:
        return model_str
    if "|" in model_str:
        provider, _, name = model_str.partition("|")
        return f"{provider.strip()}:{name.strip()}"
    return model_str


class FidesOpenCUALoop(Loop):
    """
    Wraps FidesOpenCUAAgent in a real Fides tool-calling LabeledPlanningLoop
    (mm_agents/fides/loop_and_planner.py for "full_exposure", or
    mm_agents/fides/loop_and_planner_no_exposure.py for "no_exposure"),
    mirroring the construction recipe in mm_agents/fides/run.py's test().

    reset()/loop() translate between the generic Loop interface (instruction,
    obs) driven by lib_run_single.run_single_example and the Fides-specific
    LabeledPlanningLoop.loop(msg, label) tool-calling loop.
    """

    def __init__(
        self,
        agent,
        env,
        example_result_dir,
        max_steps,
        args,
        variant: Literal["full_exposure", "no_exposure"] = "no_exposure",
        p_llm: Optional[str] = None,
        q_llm: Optional[str] = None,
        q_llm_second_check: Optional[str] = None,
        system_defense_level: int = 1,
        model_defense_level: int = 0,
        token_log_path: Optional[str] = None,
    ):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args
        self.variant = variant
        self.p_llm = _as_planner_model(p_llm or args.model)
        self.q_llm = _as_planner_model(q_llm or self.p_llm)
        q_llm_second_check = _as_planner_model(q_llm_second_check) if q_llm_second_check else None
        token_usage.register_role("q_vlm", args.model)
        token_usage.register_role("planner", self.p_llm)
        token_usage.register_role("q_llm", self.q_llm)
        token_usage.register_role("q_llm_second", q_llm_second_check)

        if variant == "no_exposure":
            from mm_agents.fides.loop_and_planner_no_exposure import LabeledBasicPlanner, LabeledPlanningLoop
        else:
            from mm_agents.fides.loop_and_planner import LabeledBasicPlanner, LabeledPlanningLoop

        python_tools = [
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
        if system_defense_level >= 1:
            python_tools.remove(run_vlm)
        if system_defense_level >= 2:
            for t in (summarize_screenshot_content, get_page_elements, get_page_text, find_element_by_text, verify_hypothesis):
                python_tools.remove(t)
        tools = [Tool.from_callable(t) for t in python_tools]

        qllm_agent = QLLMAgent(
            action_space=args.action_space,
            observation_type="a11y_tree",
            max_trajectory_length=args.max_trajectory_length,
            model=self.q_llm,
            token_log_path=token_log_path,
        )

        functions_block = SYST_DEF_1 if system_defense_level == 0 else SYST_DEF_2 if system_defense_level == 1 else SYST_DEF_3
        exposure_block = SYST_PROMPT_PLLM_NO_EXPOSURE if variant == "no_exposure" else SYST_PROMPT_PLLM_FULL_EXPOSURE
        system_prompt = build_pllm_prompt(functions_block=functions_block, exposure_block=exposure_block)

        universe = frozenset({})
        self._initial_label = ProductLabel(
            IntegrityLabel.trusted(),
            InverseLattice(PowersetLattice(subset=universe, universe=universe)),
        )

        if variant == "no_exposure":
            qllm_agent_second = (
                QLLMAgent(
                    action_space=args.action_space,
                    observation_type="a11y_tree",
                    max_trajectory_length=args.max_trajectory_length,
                    model=q_llm_second_check,
                    token_log_path=token_log_path,
                )
                if q_llm_second_check else None
            )
            planner = LabeledBasicPlanner(
                tools=tools,
                state=[{"role": "system", "content": system_prompt}],
                model=self.p_llm,
            )
            self._fides_loop = LabeledPlanningLoop(
                planner=planner,
                model=self.p_llm,
                tools=tools,
                policy=[],
                qllm_agent=qllm_agent,
                qllm_agent_second=qllm_agent_second,
                agent=agent,
                env=env,
                example_result_dir=example_result_dir,
                step_idx=0,
                max_steps=max_steps,
                observation_type=args.observation_type,
                model_defense_level=model_defense_level,
                token_log_path=token_log_path,
            )
        else:
            planner = LabeledBasicPlanner(
                state=[{"role": "system", "content": system_prompt}],
                tools=tools,
            )
            self._fides_loop = LabeledPlanningLoop(
                planner=planner,
                model=self.p_llm,
                tools=tools,
                policy=[],
                qllm_agent=qllm_agent,
                agent=agent,
                env=env,
                example_result_dir=example_result_dir,
                step_idx=0,
                max_steps=max_steps,
                observation_type=args.observation_type,
                model_defense_level=model_defense_level,
                token_log_path=token_log_path,
            )

    def reset(self, tags, runtime_logger):
        self.agent.reset(runtime_logger)
        self._fides_loop.example_result_dir = self.example_result_dir
        self._fides_loop.step_idx = 0
        self._fides_loop.turn = 0
        planner = self._fides_loop.planner
        planner.history = [planner.history[0]]
        if hasattr(planner, "memory"):
            planner.memory.clear()

    def loop(self, instruction: str, obs: dict) -> str:
        user_message = ChatCompletionUserMessageParam(role="user", content=instruction)
        response, _label = self._fides_loop.loop(user_message, label=self._initial_label)
        return response
