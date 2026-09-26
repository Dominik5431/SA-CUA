import json
import os

from mm_agents.utils.loop import Loop, run_predict_step_loop


def index_of_false_after_last_true(lst):
    last_true_index = -1
    for i in range(len(lst) - 1, -1, -1):
        if lst[i] is True:
            last_true_index = i
            break
    if last_true_index == -1 or last_true_index + 1 >= len(lst):
        return -1
    if lst[last_true_index + 1] is False:
        return last_true_index + 1
    return -1


class PromptAgentLoop(Loop):
    def __init__(self, agent, env, example_result_dir, max_steps, args):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args

    def loop(self, instruction: str, obs: dict) -> str:
        response, tool_call_is_triggered_l = run_predict_step_loop(
            self.agent, self.env, instruction, obs, self.example_result_dir, self.max_steps, self.args
        )

        if self.agent.cua:
            response = ""
            with open(os.path.join(self.example_result_dir, "response.json")) as f:
                saved_responses = json.load(f)

            index_of_first_false_after_last_true = index_of_false_after_last_true(tool_call_is_triggered_l)

            for index_saved_response, saved_response in enumerate(saved_responses):
                if index_saved_response < index_of_first_false_after_last_true:
                    continue
                response += (
                    f"Result of the Computer Use Agent call at step {index_saved_response}:"
                    + saved_response["rounds_to_call"]["first"] + "\n\n"
                )

        return response
