from mm_agents.utils.loop import Loop, run_predict_step_loop, run_predict_step_loop_with_mcp


class HybridAgentLoop(Loop):
    def __init__(self, agent, env, example_result_dir, max_steps, args):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args

    def loop(self, instruction: str, obs: dict) -> str:
        response, _ = run_predict_step_loop_with_mcp(
            self.agent, self.env, instruction, obs, self.example_result_dir, self.max_steps, self.args
        )
        return response
