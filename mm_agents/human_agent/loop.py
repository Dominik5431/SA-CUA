"""Episode loop for the human agent.

This is intentionally thin: it reuses the exact same ``run_predict_step_loop``
driver as the model-based agents, so the on-disk artifacts (per-step screenshots
and ``traj.jsonl``) are identical regardless of whether the actions came from a
model or from a human at the keyboard.
"""

from mm_agents.utils.loop import Loop, run_predict_step_loop


class HumanAgentLoop(Loop):
    def __init__(self, agent, env, example_result_dir, max_steps, args):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args

    def loop(self, instruction: str, obs: dict) -> str:
        response, _tool_call_is_triggered_l = run_predict_step_loop(
            self.agent,
            self.env,
            instruction,
            obs,
            self.example_result_dir,
            self.max_steps,
            self.args,
        )
        return response
