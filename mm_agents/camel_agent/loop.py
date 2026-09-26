from mm_agents.utils.loop import Loop, run_predict_step_loop


class CamelAgentLoop(Loop):
    def __init__(self, agent, env, example_result_dir, max_steps, args):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args
        self.agent.env = env

    def loop(self, instruction: str, obs: dict) -> str:
        response, _tool_call_is_triggered_l = run_predict_step_loop(
            self.agent, self.env, instruction, obs, self.example_result_dir, self.max_steps, self.args
        )
        return response
