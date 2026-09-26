import datetime
import json
import logging
import os
from abc import ABC, abstractmethod

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionMessage

from mm_agents import token_usage
from mm_agents.utils import mcp_tools

logger = logging.getLogger("desktopenv.experiment")


class Loop(ABC):
    def reset(self, tags, runtime_logger):
        """
        Reset the wrapped agent for a new episode. Override if the wrapped
        agent's reset() signature doesn't accept tags/_logger.
        """
        self.agent.reset(tags=tags, _logger=runtime_logger)
        self.agent.example_result_dir = self.example_result_dir

    @abstractmethod
    def loop(self, msg: ChatCompletionMessage | ChatCompletionMessageParam, *args):
        """
        Manage message / tool / action assignment
        """
        pass


def run_predict_step_loop(agent, env, instruction, obs, example_result_dir, max_steps, args):
    """
    Drive one episode for an agent implementing
    predict(instruction, obs) -> (response, actions, tool_call_triggered),
    executing each returned action against env.step and logging
    screenshots/traj.jsonl as it goes.

    Returns (last_response, tool_call_is_triggered_l).
    """
    tool_call_is_triggered_l = []
    done = False
    step_idx = 0
    response = ""
    while not done and step_idx < max_steps:
        os.environ["CURRENT_STEP"] = str(step_idx)
        agent.step_idx = step_idx + 1
        token_usage.set_step(step_idx + 1)
        response, actions, tool_call_is_triggered = agent.predict(instruction, obs)

        if isinstance(response, str):
            env.agent_responses.append(response)

        tool_call_is_triggered_l.append(tool_call_is_triggered)

        for action in actions:
            action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
            logger.info("Step %d: %s", step_idx + 1, action)
            obs, reward, done, info = env.step(action, args.sleep_after_execution)

            logger.info("Reward: %.2f", reward)
            logger.info("Done: %s", done)
            with open(os.path.join(example_result_dir, f"step_{step_idx + 1}_{action_timestamp}.png"),
                      "wb") as _f:
                _f.write(obs['screenshot'])
            with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                f.write(json.dumps({
                    "step_num": step_idx + 1,
                    "action_timestamp": action_timestamp,
                    "action": action,
                    "reward": reward,
                    "done": done,
                    "info": info,
                    "screenshot_file": f"step_{step_idx + 1}_{action_timestamp}.png"
                }))
                f.write("\n")
            if done:
                logger.info("The episode is done.")
                break
        step_idx += 1

    return response, tool_call_is_triggered_l


def is_mcp_tool_call(action) -> bool:
    return isinstance(action, dict) and action.get("action_type") == "MCP_TOOL_CALL"


def dispatch_mcp_tool_call(action: dict, ctx: mcp_tools.ToolContext) -> mcp_tools.ToolResult:
    tool = mcp_tools.get_tool(action["tool_name"])
    if tool is None:
        return mcp_tools.ToolResult(content=f"Unknown tool '{action['tool_name']}'", is_error=True)
    try:
        return tool.run(action.get("arguments", {}), ctx)
    except Exception as e:
        logger.exception("MCP tool '%s' raised", action["tool_name"])
        return mcp_tools.ToolResult(content=f"Tool '{action['tool_name']}' raised an exception: {e}", is_error=True)


def run_predict_step_loop_with_mcp(agent, env, instruction, obs, example_result_dir, max_steps, args):
    """
    Drive one episode for an agent implementing
    predict(instruction, obs) -> (response, actions, tool_call_triggered),
    executing each returned action either as an MCP tool call (dispatched via
    mm_agents.utils.mcp_tools) or against env.step, logging screenshots/traj.jsonl as it goes.

    An MCP tool call is signaled by an action shaped like
    {"action_type": "MCP_TOOL_CALL", "tool_name": ..., "arguments": {...}}. Its result is stashed
    and surfaced to the agent's next predict() call via obs["mcp_tool_result"], and no further
    actions from the same predict() batch are executed - the agent should see the tool result
    before deciding what to do next, so a predict() batch should contain at most one tool call.

    Returns (last_response, tool_call_is_triggered_l).
    """
    tool_call_is_triggered_l = []
    done = False
    step_idx = 0
    response = ""
    pending_tool_result = None
    while not done and step_idx < max_steps:
        os.environ["CURRENT_STEP"] = str(step_idx)
        agent.step_idx = step_idx + 1
        token_usage.set_step(step_idx + 1)
        obs["mcp_tool_result"] = pending_tool_result
        pending_tool_result = None

        response, actions, tool_call_is_triggered = agent.predict(instruction, obs)

        if isinstance(response, str):
            env.agent_responses.append(response)

        tool_call_is_triggered_l.append(tool_call_is_triggered)

        for action in actions:
            action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
            logger.info("Step %d: %s", step_idx + 1, action)

            if is_mcp_tool_call(action):
                ctx = mcp_tools.ToolContext(
                    env=env, agent=agent, example_result_dir=example_result_dir,
                    step_idx=step_idx + 1, args=args,
                )
                tool_result = dispatch_mcp_tool_call(action, ctx)
                logger.info("MCP tool result (is_error=%s): %s", tool_result.is_error, tool_result.content)

                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                    f.write(json.dumps({
                        "step_num": step_idx + 1,
                        "action_timestamp": action_timestamp,
                        "action": action,
                        "is_mcp_call": True,
                        "mcp_result": tool_result.to_log_dict(),
                        "reward": 0,
                        "done": False,
                        "info": {},
                        "screenshot_file": None,
                    }))
                    f.write("\n")

                pending_tool_result = {
                    "tool_name": action["tool_name"],
                    "call_id": action.get("call_id"),
                    "content": tool_result.content,
                    "is_error": tool_result.is_error,
                }
                break

            obs, reward, done, info = env.step(action, args.sleep_after_execution)

            logger.info("Reward: %.2f", reward)
            logger.info("Done: %s", done)
            with open(os.path.join(example_result_dir, f"step_{step_idx + 1}_{action_timestamp}.png"),
                      "wb") as _f:
                _f.write(obs['screenshot'])
            with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                f.write(json.dumps({
                    "step_num": step_idx + 1,
                    "action_timestamp": action_timestamp,
                    "action": action,
                    "is_mcp_call": False,
                    "reward": reward,
                    "done": done,
                    "info": info,
                    "screenshot_file": f"step_{step_idx + 1}_{action_timestamp}.png"
                }))
                f.write("\n")
            if done:
                logger.info("The episode is done.")
                break
        step_idx += 1

    return response, tool_call_is_triggered_l
