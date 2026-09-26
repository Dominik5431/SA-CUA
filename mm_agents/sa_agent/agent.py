from __future__ import annotations

import json
import logging
import os
import re

from mm_agents.sa_agent.capabilities import (
    CapabilityEnforcer,
    parse_capability_set,
)
from mm_agents.sa_agent.llm import _LLMClient, build_client, encode_screenshot
from mm_agents.sa_agent import prompts
from prompt_agent.agent import linearize_accessibility_tree, trim_accessibility_tree

logger = logging.getLogger("desktopenv.SA")

_CODE_BLOCK_RE = re.compile(r"```python\s*(.*?)\s*```", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_TERMINAL_RE = re.compile(r"\b(DONE|FAIL)\b")


def _parse_qvlm_response(response: str) -> tuple[str | None, bool]:
    """
    Extract action and terminal status from a Q-VLM response.
    Returns (action_code | None, is_terminal).

    is_terminal is True when:
    - the response contains DONE or FAIL, OR
    - the response contains a ```json block (the structured summary) even
      without the explicit keyword — the model sometimes skips it.
    """
    is_terminal = bool(_TERMINAL_RE.search(response)) or bool(_JSON_BLOCK_RE.search(response))
    code_match = _CODE_BLOCK_RE.search(response)
    action = code_match.group(1).strip() if code_match else None
    return action, is_terminal


def _parse_summary(response: str, return_schema: dict | None = None) -> dict:
    """
    Extract a structured summary JSON block from a Q-VLM response string.
    Falls back to a synthetic FAIL summary if no valid JSON block is found.
    """
    json_match = _JSON_BLOCK_RE.search(response)
    if json_match:
        try:
            summary = json.loads(json_match.group(1))
            if isinstance(summary, dict):
                return summary
        except json.JSONDecodeError:
            pass
    return {
        "status": "FAIL",
        "goal_met": False,
        "findings": response[:500] if response else "",
        "extracted": {},
        "actions_taken": [],
        "blocked_actions": [],
        "confidence": 0.0,
        "needs": "executor did not produce a structured JSON summary",
    }


class SAAgent:
    """
    Capability-Gated Execution Agent (SA-Agent).

    Implements a Dual-LLM architecture: a Privileged Planner (P-LLM) that never
    observes raw screen content decomposes tasks into capability-scoped sub-tasks,
    and a Quarantined Executor (Q-VLM) that carries out each sub-task one action
    at a time within a mechanically-enforced capability set.

    Follows the standard step-by-step predict() pattern — lib_run_single.py
    controls env.step(); the agent does NOT need agent.env access.
    """

    cua: bool = False

    def __init__(
        self,
        planner_model: str,
        executor_model: str,
        verifier_model: str | None = None,
        max_steps: int = 15,
        max_retries_per_subtask: int = 3,
        max_executor_actions: int = 20,
        observation_type: str = "screenshot",
        action_space: str = "pyautogui",
        sleep_after_execution: float = 1.0,
        **kwargs,
    ):
        self.planner_model = planner_model
        self.executor_model = executor_model
        self.verifier_model = verifier_model
        self.max_steps = max_steps
        self.max_retries_per_subtask = max_retries_per_subtask
        self.max_executor_actions = max_executor_actions
        self.observation_type = observation_type
        self.action_space = action_space
        self.sleep_after_execution = sleep_after_execution
        self.a11y_tree_max_tokens = 10000

        self._pllm_client: _LLMClient | None = None
        self._qvlm_client: _LLMClient | None = None
        self._verifier_client: _LLMClient | None = None

        self.safety_check_cnt: int = 0
        self.step_idx: int = 0
        self.example_result_dir: str | None = None

        self.thoughts: list = []
        self.actions: list = []
        self.observations: list = []

        self._episode_step: int = 0
        self._planner_done: bool = False
        self._planner_history: list[dict] = []
        self._current_subtask: dict | None = None
        self._enforcer: CapabilityEnforcer | None = None
        self._qvlm_messages: list[dict] = []
        self._subtask_action_count: int = 0
        self._subtask_actions_taken: list[str] = []
        self._last_step_blocked: bool = False
        self._last_blocked_reason: str = ""
        self._subtask_retries: int = 0


    def reset(self, tags=None, _logger=None):
        self.safety_check_cnt = 0
        self._episode_step = 0
        self._planner_done = False
        self._planner_history = []
        self._current_subtask = None
        self._enforcer = None
        self._qvlm_messages = []
        self._subtask_action_count = 0
        self._subtask_actions_taken = []
        self._last_step_blocked = False
        self._last_blocked_reason = ""
        self._subtask_retries = 0
        self.thoughts.clear()
        self.actions.clear()
        self.observations.clear()
        if _logger is not None:
            for handler in _logger.handlers:
                if handler not in logger.handlers:
                    logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)

    def predict(self, instruction: str, obs: dict) -> tuple[str, list, bool]:
        """
        One step of the Dual-LLM delegation loop.

        Returns (response_str, actions_list, tool_call_triggered=False).
        - actions_list == ["DONE"] signals episode completion.
        - actions_list == [] means no action this step (blocked or sub-task boundary).
        - actions_list == [pyautogui_code] means one executable action.
        """
        if self._planner_done:
            return "done", ["DONE"], False

        self._episode_step += 1
        if self._episode_step > self.max_steps:
            logger.warning("[SA] Step budget (%d) exhausted.", self.max_steps)
            self._planner_done = True
            return f"FAIL: step budget exhausted after {self.max_steps} steps", ["DONE"], False

        if self._pllm_client is None:
            self._pllm_client = build_client(self.planner_model)
        if self._qvlm_client is None:
            self._qvlm_client = build_client(self.executor_model)
        if self._verifier_client is None:
            if not self.verifier_model:
                self._verifier_client = build_client(self.executor_model)
            else:
                self._verifier_client = build_client(self.verifier_model)

        if self._current_subtask is None:
            subtask = self._call_planner(instruction, self._planner_history)
            self._save_trace({"phase": "planner", "step": self._episode_step, "subtask": subtask})
            logger.info("[SA] Planner subtask: %s", json.dumps(subtask))

            if subtask.get("is_done"):
                self._planner_done = True
                final = subtask.get("final_answer", "Task complete.")
                logger.info("[SA] Planner declared task done: %s", final)
                return final, ["DONE"], False

            self._current_subtask = subtask
            self._enforcer = CapabilityEnforcer(
                parse_capability_set(subtask.get("capability_set") or [])
            )
            self._qvlm_messages = []
            self._subtask_action_count = 0
            self._subtask_actions_taken = []
            self._last_step_blocked = False
            self._subtask_retries = 0

        screenshot_b64 = encode_screenshot(obs["screenshot"])

        a11y_text: str | None = None
        raw_a11y = obs.get("accessibility_tree") if isinstance(obs, dict) else None
        if raw_a11y and self.observation_type in ("a11y_tree", "screenshot_a11y_tree"):
            a11y_text = linearize_accessibility_tree(raw_a11y)
            a11y_text = trim_accessibility_tree(a11y_text, self.a11y_tree_max_tokens)

        if not self._qvlm_messages:
            system_msg = prompts.build_qvlm_system_message(self._current_subtask)
            user_msg = prompts.build_qvlm_initial_user_message(
                screenshot_b64,
                goal=self._current_subtask.get("goal", ""),
                a11y_tree=a11y_text,
            )
            self._qvlm_messages = [system_msg, user_msg]
        elif self._last_step_blocked:
            user_msg = prompts.build_qvlm_blocked_message(
                self._last_blocked_reason, screenshot_b64, a11y_tree=a11y_text
            )
            self._qvlm_messages.append(user_msg)
        elif obs.get("error"):
            user_msg = prompts.build_qvlm_error_message(
                obs["error"], screenshot_b64, a11y_tree=a11y_text
            )
            self._qvlm_messages.append(user_msg)
        else:
            user_msg = prompts.build_qvlm_continuation_message(screenshot_b64, a11y_tree=a11y_text)
            self._qvlm_messages.append(user_msg)

        raw_response = self._qvlm_client.chat(self._qvlm_messages, max_tokens=4096, temperature=0.5)
        print(f"[SA] Raw Q-VLM response (step {self._episode_step}):\n{raw_response}\n", flush=True)
        response = self._strip_thinking(raw_response)
        self._qvlm_messages.append({"role": "assistant", "content": response})
        self.thoughts.append(response)

        action, is_subtask_done = _parse_qvlm_response(response)

        budget_hit = self._subtask_action_count >= self.max_executor_actions
        if is_subtask_done or budget_hit:
            if budget_hit and not is_subtask_done:
                logger.warning(
                    "[SA] Sub-task executor action budget (%d) exhausted.",
                    self.max_executor_actions,
                )
            summary = _parse_summary(response, self._current_subtask.get("return_schema"))
            summary["actions_taken"] = self._subtask_actions_taken
            summary["blocked_actions"] = self._enforcer.blocked_actions if self._enforcer else []

            if self._verifier_client is not None:
                verdict = self._verify(summary, obs["screenshot"])
                logger.info("[SA] Verifier verdict: %s", verdict)
                self._save_trace({"phase": "verifier", "step": self._episode_step, "verdict": verdict})
                if verdict.get("verdict") == "ATTACKED":
                    msg = f"[SA] HALT: verifier detected branch-steering attack. {verdict.get('reason', '')}"
                    logger.warning(msg)
                    print(msg, flush=True)
                    self._planner_done = True
                    return msg, ["DONE"], False

            self._planner_history.append({
                "subtask": self._current_subtask,
                "summary": summary,
                "blocked_actions": self._enforcer.blocked_actions if self._enforcer else [],
                "destructive_actions": self._enforcer.destructive_actions_taken if self._enforcer else [],
            })
            self._save_trace({
                "phase": "subtask_complete",
                "step": self._episode_step,
                "subtask": self._current_subtask,
                "summary": summary,
            })
            self._current_subtask = None
            return response, [], False

        if action:
            allowed, reason = (
                self._enforcer.check(action)
                if self._enforcer else (True, "OK")
            )
            if not allowed:
                self._last_step_blocked = True
                self._last_blocked_reason = reason
                self._save_trace({"phase": "blocked", "step": self._episode_step, "action": action, "reason": reason})
                logger.info("[SA] Action blocked: %s — %s", action[:80], reason)
                return response, [], False

            self._last_step_blocked = False
            self._subtask_action_count += 1
            self._subtask_actions_taken.append(action)
            self.actions.append([action])
            self._save_trace({"phase": "action", "step": self._episode_step, "action": action})
            return response, [action], False

        self._last_step_blocked = False
        return response, [], False


    @staticmethod
    def _strip_thinking(raw: str) -> str:
        """
        Remove reasoning-model thinking tokens from a response string.
        When enable_thinking=True, vLLM injects <think> as an input prefix so
        the content field starts mid-thought and ends with </think> before the
        actual answer.  Also handles the explicit <think>...</think> wrapper.
        """
        if "</think>" in raw:
            return raw.split("</think>", 1)[1].strip()
        return raw.strip()

    @staticmethod
    def _extract_json_candidate(text: str) -> str:
        """Return the first balanced {...} substring in text, or text itself."""
        start = text.find("{")
        if start == -1:
            return text
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return text[start:]

    def _call_planner(self, task: str, history: list[dict]) -> dict:
        """Call P-LLM and parse JSON subtask. Retries once on parse failure."""
        messages = prompts.build_pllm_messages(task, history)
        for attempt in range(2):
            try:
                raw = self._pllm_client.chat(messages, max_tokens=4096, temperature=0.2)
                print(f"[SA] Raw planner response (attempt {attempt + 1}):\n{raw}\n", flush=True)
                clean = self._strip_thinking(raw)
                if not clean:
                    clean = self._extract_json_candidate(raw)
                if clean.startswith("```"):
                    clean = re.sub(r"^```[a-z]*\s*", "", clean)
                    clean = re.sub(r"\s*```$", "", clean.strip())
                return json.loads(clean)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("[SA] Planner parse error (attempt %d): %s", attempt + 1, exc)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": "Your response was not valid JSON. Please respond with only a JSON object.",
                    })
        logger.error("[SA] Planner failed to return valid JSON after 2 attempts.")
        return {"is_done": True, "final_answer": "ABORT: planner failed to return valid JSON."}

    def _verify(self, summary: dict, screenshot_bytes: bytes) -> dict:
        """Call verifier model and return verdict dict."""
        screenshot_b64 = encode_screenshot(screenshot_bytes)
        goal = self._current_subtask.get("goal", "") if self._current_subtask else ""
        messages = prompts.build_verifier_messages(goal, summary, screenshot_b64)
        try:
            raw = self._verifier_client.chat(messages, max_tokens=256, temperature=0.0)
            clean = raw.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-z]*\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)
            return json.loads(clean)
        except Exception as exc:
            logger.warning("[SA] Verifier call failed: %s", exc)
            return {"verdict": "UNVERIFIABLE", "reason": str(exc)}

    def _save_trace(self, data: dict) -> None:
        """Append a trace entry to sa_trace.jsonl in example_result_dir."""
        if not self.example_result_dir:
            return
        trace_path = os.path.join(self.example_result_dir, "sa_trace.jsonl")
        try:
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, default=str) + "\n")
        except OSError as exc:
            logger.warning("[SA] Could not write trace: %s", exc)
